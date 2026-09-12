"""Two-ended port editing and truthful legacy Activity, without cloud writes."""
import curses
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import nebius_core as core
import nebius_jobs as jobs
import nebius_ports as ports
import nebius_ui as ui
from test_ui import app


class PortFormTests(unittest.TestCase):
    def test_ports_refreshes_connection_status_without_manual_reload(self):
        application, screen = app([None, None, "\x1b"])
        mapping = {"id": "forward-one", "vm_id": "computeinstance-test", "vm_name": "inference",
                   "local_port": 18000, "remote_port": 8000, "enabled": True, "state": "Connecting"}
        with patch.object(ports, "listing", side_effect=[[mapping], [{**mapping, "state": "Connected"}]]), \
             patch.object(ui.time, "monotonic", side_effect=[0, 0, 0, 3, 3]):
            with self.assertRaises(ui.Back):
                application.ports()
        self.assertIn("Connecting", screen.frames[0])
        self.assertIn("Connected", screen.frames[-1])
        self.assertIn("Updates every 2s", screen.frames[-1])

    def test_live_refresh_keeps_add_port_selected_when_mapping_appears(self):
        application, screen = app([None, "\n"])
        initial = [("Add port", "", "add")]
        refreshed = [("New forward", "Connected", {"id": "new"}), *initial]
        with patch.object(ui.time, "monotonic", side_effect=[0, 0, 3, 3]):
            self.assertEqual(application.menu("Ports", lambda: refreshed if screen.frames else initial), "add")

    def form(self, keys, width=80, height=24, **kwargs):
        application, screen = app(keys, width, height)
        with patch.object(ports, "mappings", return_value=[]), patch.object(ports, "available", return_value=True):
            result = application.port_form({"name": "inference"}, **kwargs)
        return result, screen

    def test_both_ports_and_route_are_visible_on_one_page(self):
        for width, height in [(48, 20), (80, 24), (120, 44)]:
            result, screen = self.form(["\n"], width, height)
            self.assertEqual(result, (8000, 8000))
            frame = screen.frames[-1]
            for text in ("Remote port", "Local port", "This computer", "VM app", "SSH", "127.0.0.1:8000", "[Tab/↑↓]", "[Enter] save", "[Esc] cancel"):
                self.assertIn(text, frame)

    def test_tab_or_arrows_switch_fields_and_preserve_both_edits(self):
        for key in ("\t", curses.KEY_UP, curses.KEY_DOWN, curses.KEY_BTAB):
            result, screen = self.form([*"8188", key, *"18188", "\n"])
            self.assertEqual(result, (8188, 18188))
            self.assertIn("127.0.0.1:18188", screen.frames[-1])
            self.assertIn("127.0.0.1:8188", screen.frames[-1])

    def test_default_local_port_follows_remote_until_edited(self):
        result, _ = self.form([*"8188", "\n"])
        self.assertEqual(result, (8188, 8188))
        result, _ = self.form([*"80", "\n"])
        self.assertEqual(result, (80, 8080))
        result, _ = self.form(["\t", *"18000", "\t", *"9000", "\n"])
        self.assertEqual(result, (9000, 18000))

    def test_horizontal_arrows_edit_without_changing_fields(self):
        result, _ = self.form([curses.KEY_LEFT, curses.KEY_BACKSPACE, "9", "\n"])
        self.assertEqual(result, (8090, 8090))

    def test_occupied_local_port_stays_inline_and_preserves_remote(self):
        application, screen = app([*"8188", "\t", *"18000", "\n", *"18001", "\n"], 48, 20)
        with patch.object(ports, "mappings", return_value=[]), patch.object(ports, "available", side_effect=lambda p: p != 18000):
            self.assertEqual(application.port_form({"name": "inference"}), (8188, 18001))
        self.assertIn("in use", "\n".join(screen.frames))
        self.assertIn("127.0.0.1:8188", screen.frames[-1])

    def test_invalid_remote_port_is_not_submitted(self):
        result, screen = self.form([*"65536", "\n", *"8188", "\n"], 48, 20)
        self.assertEqual(result, (8188, 8188))
        self.assertIn("Remote port: Choose a port", "\n".join(screen.frames))

    def test_escape_never_installs_a_forward(self):
        application, _ = app(["\x1b"])
        with patch.object(ports, "add") as add, patch.object(ports, "install") as install:
            with self.assertRaises(ui.Back):
                application.port_form({"name": "inference"})
        add.assert_not_called()
        install.assert_not_called()

    def test_ports_flow_saves_only_after_one_form(self):
        application, screen = app(["n", *"8188", "\t", *"18188", "\n", "\x1b"])
        vm = {"id": "computeinstance-test", "name": "inference"}
        with patch.object(ports, "listing", return_value=[]), patch.object(ports, "mappings", return_value=[]), \
             patch.object(ports, "available", return_value=True), patch.object(application, "read", return_value={}) as read:
            with self.assertRaises(ui.Back):
                application.ports(vm)
        read.assert_called_once_with("Saving SSH port forward", "ports", "--action", "add", "--vm-id", vm["id"],
                                     "--remote-port", "8188", "--local-port", "18188")


class ActivityHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for key, value in {"STATE_DIR": self.root, "OPERATION_FILE": self.root / "operation.json"}.items():
            context = patch.object(core, key, value)
            context.start()
            self.addCleanup(context.stop)
        self.job_id = "a" * 24

    def save(self, **fields):
        job = {"id": self.job_id, "command": "create", "phase": "ready", **fields}
        core._atomic_json(self.root / "jobs" / (self.job_id + ".json"), job)
        return job

    def test_old_job_recovers_its_own_completed_operation(self):
        job = self.save(result={"name": "h100"})
        operation = {"phase": "ready", "message": "VM created and running"}
        (self.root / "activity.log").write_text(json.dumps({**job, "operation": operation}) + "\n")
        core._atomic_json(core.OPERATION_FILE, {"phase": "running", "message": "Somebody else's operation"})
        self.assertEqual(jobs.jobs()[0]["operation"], operation)
        view = jobs.describe(jobs.jobs()[0])
        self.assertEqual(view["title"], "Create VM · h100")
        self.assertEqual(view["status"], "Completed")
        self.assertEqual(jobs.summary()["message"], "VM created and running")
        self.assertEqual(core._read_json(self.root / "jobs" / (self.job_id + ".json"), {}), job)

    def test_completion_without_progress_file_uses_saved_result(self):
        self.save(command="delete", result={"name": "training", "deleted": True, "disk_deleted": True})
        view = jobs.describe(jobs.jobs()[0])
        self.assertEqual(view["title"], "Delete VM · training")
        self.assertEqual(view["message"], "VM and boot disk deleted")
        self.assertNotIn("worker", json.dumps(view).lower())

    def test_modern_sidecar_wins_over_historical_log(self):
        job = self.save()
        (self.root / "activity.log").write_text(json.dumps({**job, "operation": {"message": "old"}}) + "\n")
        core._atomic_json(self.root / "jobs" / (self.job_id + ".operation.json"), {"phase": "ready", "message": "current"})
        self.assertEqual(jobs.jobs()[0]["operation"]["message"], "current")

    def test_broken_log_lines_and_embedded_results_are_supported(self):
        self.save(operation={"phase": "ready", "message": "Done"})
        (self.root / "activity.log").write_text("partial-json\n[]\n{}\n")
        self.assertEqual(jobs.jobs()[0]["operation"]["message"], "Done")

    def test_status_fallbacks_are_phase_specific(self):
        for phase, status in [("ready", "Completed"), ("error", "Failed"), ("running", "In progress"), ("queued", "Queued"), ("interrupted", "Check outcome")]:
            view = jobs.describe({"phase": phase, "command": "stop"})
            self.assertEqual(view["status"], status)
            self.assertNotIn("worker", view["message"].lower())

    def test_old_failed_jobs_cannot_offer_resume_without_request_arguments(self):
        job = {"phase": "error", "command": "delete"}
        self.assertFalse(jobs.can_resume(job))
        self.assertFalse(jobs.can_resume({**job, "arguments": ["delete", "--vm-id", "computeinstance-test"]}))
        self.assertTrue(jobs.can_resume({**job, "arguments": ["delete", "--vm-id", "computeinstance-test", "--confirmed"]}))

    def test_completed_activity_displays_a_result_without_an_extra_menu(self):
        job = self.save(result={"name": "h100"})
        application, screen = app(["\n", "\n", "\x1b", "\x1b"], 48, 20)
        with self.assertRaises(ui.Back):
            application.activity()
        frames = "\n".join(screen.frames)
        self.assertIn("Completed", frames)
        self.assertIn("VM created", frames)
        self.assertNotIn("Waiting for worker", frames)
        self.assertNotIn("Operation result", frames)

    def test_live_refresh_retains_selection_and_search(self):
        application, screen = app(["j", None, "\n"])
        first = [("One", "running", {"id": "one"}), ("Two", "running", {"id": "two"})]
        refreshed = [("New", "queued", {"id": "new"}), ("One", "done", {"id": "one"}), ("Two", "done", {"id": "two"})]
        with patch.object(ui.time, "monotonic", side_effect=[0, 0, 0, 3, 3]), patch.object(jobs, "jobs"):
            self.assertEqual(application.menu("Activity", lambda: refreshed if len(screen.frames) >= 2 else first), {"id": "two"})
        self.assertIn("done", screen.frames[-1])


if __name__ == "__main__":
    unittest.main()
