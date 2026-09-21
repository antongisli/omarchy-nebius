"""Cloud-backed nicknames without changing VM identities or unrelated metadata."""
import contextlib
import copy
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import nebius_core as core
import nebius_inventory as inventory
import nebius_job as worker
import nebius_jobs as jobs
import nebius_ui as ui
from test_ui import app

VM = {"id": "computeinstance-example", "name": "nebius-h100-1234", "nickname": "",
      "state": "running", "region": "eu-north1", "project_name": "personal",
      "platform": "gpu-h100-sxm", "allocation": "on_demand", "can_delete": True}


class NicknameCoreTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(contextlib.ExitStack())
        temporary = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(patch.object(core, "STATE_DIR", Path(temporary)))
        self.progress = stack.enter_context(patch.object(core, "_write_operation"))
        self.vm = dict(VM)
        self.instance = {"metadata": {"id": VM["id"], "name": VM["name"], "resource_version": "17",
                                     "labels": {"managed-by": core.MANAGED_BY, "unrelated": "keep"}}}
        self.access = stack.enter_context(patch.object(core, "_accessible_vm", return_value=(self.instance, self.vm)))
        self.cli = stack.enter_context(patch.object(core, "run_cli"))

    def save(self, nickname):
        saved = copy.deepcopy(self.instance)
        if nickname:
            saved["metadata"]["labels"][core.NICKNAME_LABEL] = nickname.strip()
        self.cli.side_effect = ["", saved]
        return core.set_vm_nickname(VM["id"], nickname)

    def test_save_quotes_one_map_entry_and_preserves_identity(self):
        nickname = 'My "GPU", other=label / café'
        original = copy.deepcopy(self.instance)
        result = self.save(nickname)
        args = self.cli.call_args_list[0].args[0]
        self.assertEqual(next(csv.reader(io.StringIO(args[args.index("--labels-add") + 1]))),
                         [core.NICKNAME_LABEL + "=" + nickname])
        self.assertEqual(args[:4], ["compute", "instance", "update", VM["id"]])
        self.assertEqual(args[args.index("--resource-version") + 1], "17")
        self.assertNotIn("--labels", args)
        self.assertNotIn("--name", args)
        self.assertNotIn("--full", args)
        self.assertEqual(self.instance, original)
        self.assertEqual(result, {"id": VM["id"], "name": VM["name"], "nickname": nickname})
        self.assertEqual(self.cli.call_args_list[1].args[0][:3], ["compute", "instance", "get"])

    def test_removing_only_our_label(self):
        self.vm["nickname"] = "Old nickname"
        result = self.save("")
        self.assertEqual(result["nickname"], "")
        args = self.cli.call_args_list[0].args[0]
        self.assertEqual(args[args.index("--labels-remove") + 1], core.NICKNAME_LABEL)
        self.assertNotIn("--labels-add", args)

    def test_unchanged_name_does_not_write(self):
        self.vm["nickname"] = "Same"
        self.assertEqual(core.set_vm_nickname(VM["id"], " Same ")["nickname"], "Same")
        self.cli.assert_not_called()

    def test_validation_before_cloud_access(self):
        for bad in ["x" * 65, "line\nfeed", "tab\there", "\x1b[31m", "text\u202e"]:
            with self.subTest(value=bad), self.assertRaises(core.NebiusError):
                core.set_vm_nickname(VM["id"], bad)
        self.access.assert_not_called()
        self.cli.assert_not_called()
        self.assertEqual(core.validate_nickname("   "), "")
        self.assertEqual(core.validate_nickname("GPU 🎨"), "GPU 🎨")

    def test_reject_managed_busy_and_recovery_vms(self):
        for changes in [{"service_managed_by": "nodegroup"}, {"kubernetes_node": True},
                        {"state": "deleting"}, {"state": "creating"}, {"recovery_id": "saved"}]:
            self.vm.clear()
            self.vm.update(VM, **changes)
            with self.subTest(changes=changes), self.assertRaises(core.NebiusError):
                core.set_vm_nickname(VM["id"], "New")
        self.cli.assert_not_called()

    def test_pending_operation_blocks_write(self):
        core._cloud_operation_path(VM["id"]).parent.mkdir(parents=True, exist_ok=True)
        core._cloud_operation_path(VM["id"]).touch()
        with self.assertRaisesRegex(core.NebiusError, "current operation"):
            core.set_vm_nickname(VM["id"], "New")
        self.cli.assert_not_called()

    def test_resource_version_required(self):
        for version in [None, 0, "bad"]:
            self.instance["metadata"]["resource_version"] = version
            with self.assertRaisesRegex(core.NebiusError, "resource version"):
                core.set_vm_nickname(VM["id"], "New")
        self.cli.assert_not_called()

    def test_failed_or_uncertain_save_never_reports_success_or_retries(self):
        for responses in [[core.NebiusError("expired token")], ["", core.NebiusError("timeout")], ["", self.instance]]:
            self.cli.reset_mock()
            self.progress.reset_mock()
            self.cli.side_effect = responses
            with self.assertRaisesRegex(core.NebiusError, "Refresh Your VMs"):
                core.set_vm_nickname(VM["id"], "New")
            self.assertEqual(self.cli.call_count, len(responses))
            self.assertNotIn("ready", [call.args[0] for call in self.progress.call_args_list])

    def test_access_check_is_required(self):
        self.access.side_effect = core.NebiusError("outside your personal projects")
        with self.assertRaisesRegex(core.NebiusError, "outside"):
            core.set_vm_nickname(VM["id"], "New")
        self.cli.assert_not_called()

    def test_worker_dispatch_and_per_vm_lock(self):
        arguments = ["set-nickname", "--vm-id", VM["id"], "--nickname=-My GPU"]
        self.assertEqual(core.mutation_resource(arguments), VM["id"])
        self.cli.side_effect = ["", {"metadata": {"labels": {core.NICKNAME_LABEL: "-My GPU"}}}]
        with patch.object(sys, "argv", ["worker", "a" * 24, *arguments]), patch.dict("os.environ", {}):
            self.assertEqual(worker.run_job(), 0)
        job = json.loads((core.STATE_DIR / "jobs" / ("a" * 24 + ".json")).read_text())
        self.assertEqual(job["phase"], "ready")
        self.assertEqual(job["result"]["nickname"], "-My GPU")

    def test_inventory_reads_cloud_label_not_stale_local_alias(self):
        project = {"project_id": "project-example", "project_name": "personal", "region": "eu-north1"}
        with patch.object(core, "_registry", return_value={"vms": [{**VM, "nickname": "stale"}]}), \
             patch.object(core, "_pending_launches", return_value=[]), patch.object(core, "_read_json", return_value={}):
            self.assertEqual(core._vm_summary(self.instance, project)["nickname"], "")
            self.instance["metadata"]["labels"][core.NICKNAME_LABEL] = "ComfyUI"
            summary = core._vm_summary(self.instance, project)
            self.assertEqual(summary["nickname"], "ComfyUI")
            self.assertEqual(summary["name"], VM["name"])


class NicknameUITests(unittest.TestCase):
    def test_n_shortcut_saves_and_escape_cancels(self):
        application, _ = app(["n", *"ComfyUI", "\n"])
        vm = dict(VM)
        with patch.object(application, "mutate", return_value={"nickname": "ComfyUI"}) as mutate:
            application.vm_actions(vm)
            self.assertEqual(mutate.call_args.args[1:], ("set-nickname", "--vm-id", VM["id"], "--nickname=ComfyUI"))
            self.assertEqual(vm["nickname"], "ComfyUI")
        application, _ = app(["n", "\x1b"])
        with patch.object(application, "mutate") as mutate, self.assertRaises(ui.Back):
            application.vm_actions(vm)
        mutate.assert_not_called()

    def test_clear_and_unchanged_enter(self):
        for keys, expected_calls in [(["\x15", "\n"], 1), (["\n"], 0)]:
            application, _ = app(keys)
            with patch.object(application, "mutate", return_value={"nickname": ""}) as mutate:
                application.vm_actions({**VM, "nickname": "Old"}, "nickname")
                self.assertEqual(mutate.call_count, expected_calls)
                if expected_calls:
                    self.assertEqual(mutate.call_args.args[-1], "--nickname=")

    def test_failed_save_keeps_original_display(self):
        application, _ = app([*"New", "\n"])
        vm = dict(VM)
        with patch.object(application, "mutate", side_effect=ui.Back), self.assertRaises(ui.Back):
            application.vm_actions(vm, "nickname")
        self.assertEqual(vm["nickname"], "")

    def test_narrow_editor_keeps_cursor_and_instructions_visible(self):
        for width in [48, 80]:
            application, screen = app(["\n"], width=width, height=20)
            nickname = "界" * 64
            with patch.object(application, "mutate") as mutate:
                application.vm_actions({**VM, "nickname": nickname}, "nickname")
            mutate.assert_not_called()
            self.assertIn("▏", screen.frames[-1])
            self.assertIn("Leave blank to remove", " ".join(screen.frames[-1].split()))
            self.assertIn("save", screen.frames[-1])

    def test_overview_shows_both_names_and_n_opens_editor(self):
        application, screen = app(["n", "\x1b", "\x1b"])
        application.inventory = {"vms": [{**VM, "nickname": "ComfyUI"}]}
        poller = Mock(error="", process=None, poll=lambda snapshot, entries, **kwargs: snapshot)
        with patch.object(jobs, "jobs", return_value=[]), patch.object(application, "mutate") as mutate:
            application._overview(poller)
        mutate.assert_not_called()
        self.assertIn("ComfyUI · RUNNING", screen.frames[0])
        self.assertIn("VM: " + VM["name"], screen.frames[0])
        self.assertTrue(any("Leave blank to remove" in frame for frame in screen.frames))

    def test_nickname_waits_for_existing_job(self):
        application, _ = app([])
        with patch.object(application, "watch") as watch, patch.object(application, "edit") as edit:
            application.vm_actions({**VM, "operation_job_id": "job"}, "nickname")
        watch.assert_called_once()
        edit.assert_not_called()

    def test_long_nickname_keeps_vm_state_visible(self):
        application, screen = app(["\x1b"], width=48, height=20)
        application.inventory = {"vms": [{**VM, "nickname": "My studio " + "x" * 50}]}
        poller = Mock(error="", process=None, poll=lambda snapshot, entries, **kwargs: snapshot)
        with patch.object(jobs, "jobs", return_value=[]):
            application._overview(poller)
        self.assertIn("RUNNING", screen.frames[-1])

    def test_validation_stays_in_editor_until_corrected(self):
        application, screen = app([*('x' * 65), "\n", "\x15", *"Fixed", "\n"], width=48, height=20)
        with patch.object(application, "mutate", return_value={"nickname": "Fixed"}) as mutate:
            application.vm_actions(dict(VM), "nickname")
        mutate.assert_called_once()
        self.assertEqual(mutate.call_args.args[-1], "--nickname=Fixed")
        self.assertTrue(any("64 characters or fewer" in frame for frame in screen.frames))

    def test_search_matches_nickname_and_original_name(self):
        for query in ["Comfy", "h100-1234"]:
            application, screen = app(["/", *query, "\n", "\x1b"])
            application.inventory = {"vms": [{**VM, "nickname": "ComfyUI"},
                                             {**VM, "id": "computeinstance-other", "name": "Other"}]}
            poller = Mock(error="", process=None, poll=lambda snapshot, entries, **kwargs: snapshot)
            with patch.object(jobs, "jobs", return_value=[]):
                application._overview(poller)
            self.assertIn("ComfyUI", screen.frames[-1])
            self.assertIn("1 of 1", screen.frames[-1])
            self.assertNotIn("Other", screen.frames[-1])


class NicknameJobOverlayTests(unittest.TestCase):
    def test_progress_and_success_never_change_lifecycle_state(self):
        snapshot = {"vms": [VM], "updated_at": "2026-01-01"}
        job = {"id": "job", "command": "set-nickname", "arguments": ["set-nickname", "--vm-id", VM["id"]],
               "phase": "running", "started_at": "2026-01-02"}
        row = inventory.apply_jobs(snapshot, [job])["vms"][0]
        self.assertEqual(row["state"], "running")
        self.assertEqual(row["operation_job_id"], "job")
        self.assertIn("Saving nickname", row["operation_note"])
        for nickname in ["ComfyUI", ""]:
            done = {**job, "phase": "ready", "finished_at": "2026-01-03", "result": {"nickname": nickname}}
            row = inventory.apply_jobs(snapshot, [done])["vms"][0]
            self.assertEqual(row["nickname"], nickname)
            self.assertEqual(row["state"], "running")
            newer = {"vms": [{**VM, "nickname": "Changed elsewhere"}], "updated_at": "2026-01-04"}
            self.assertEqual(inventory.apply_jobs(newer, [done])["vms"][0]["nickname"], "Changed elsewhere")
            self.assertEqual(jobs.describe(done)["message"], "Nickname saved" if nickname else "Nickname removed")


if __name__ == "__main__":
    unittest.main()
