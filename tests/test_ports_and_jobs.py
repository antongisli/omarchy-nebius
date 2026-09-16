"""Concurrency, recovery and loopback safety without cloud or systemd mutations."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import nebius_core as core
import nebius_jobs as jobs
import nebius_ports as ports
import nebius_ui as ui
from test_ui import app


class PortsAndJobsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for key, path in {"STATE_DIR": self.root, "PLAN_DIR": self.root / "plans",
                          "REGISTRY_FILE": self.root / "vms.json",
                          "OPERATION_FILE": self.root / "operation.json"}.items():
            context = patch.object(core, key, path)
            context.start()
            self.addCleanup(context.stop)
        self.vm = {"id": "computeinstance-test", "name": "test", "ssh_user": "dev", "managed": True,
                   "public_ip": "192.0.2.1", "state": "running"}

    def test_registry_updates_and_deletion_do_not_overwrite_other_vms(self):
        core._update_vm_record("old", {"name": "old"})
        def update(index):
            core._update_vm_record(str(index), {"name": str(index)})
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(update, range(40)))
        core._update_vm_record("old", remove=True)
        self.assertEqual({v["id"] for v in core._registry()["vms"]}, {str(i) for i in range(40)})

    def test_lifecycle_locks_only_its_own_vm(self):
        with core.mutation_guard(resource="computeinstance-a"):
            def attempt(resource):
                with core.mutation_guard(resource=resource):
                    return True
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertTrue(pool.submit(attempt, "computeinstance-b").result())
                with self.assertRaisesRegex(core.NebiusError, "this resource"):
                    pool.submit(attempt, "computeinstance-a").result()

    def test_vm_creates_lock_per_plan_but_shared_disks_stay_serialized(self):
        first, second, third = "a" * 24, "b" * 24, "c" * 24
        core._atomic_json(core.PLAN_DIR / f"{first}.json", {"plan_id": first})
        core._atomic_json(core.PLAN_DIR / f"{second}.json", {"plan_id": second})
        for plan_id in (first, third):
            core._atomic_json(core.PLAN_DIR / f"{plan_id}.json", {
                "plan_id": plan_id, "reusable_disk": {"disk_id": "computedisk-shared"},
            })
        first_resource = core.mutation_resource(["create", "--plan-id", first])
        second_resource = core.mutation_resource(["create", "--plan-id", second])
        third_resource = core.mutation_resource(["create", "--plan-id", third])
        self.assertNotEqual(first_resource, second_resource)
        self.assertEqual(first_resource, third_resource)
        with core.mutation_guard(resource=first_resource):
            with core.mutation_guard(resource=second_resource):
                pass
            def take_shared_disk():
                with core.mutation_guard(resource=third_resource):
                    return True
            with ThreadPoolExecutor(max_workers=1) as pool, \
                 self.assertRaisesRegex(core.NebiusError, "this resource"):
                pool.submit(take_shared_disk).result()

    def test_active_launch_is_not_mistaken_for_recovery(self):
        job_id = "d" * 24
        plan = {"plan_id": "p" * 24, "operation_job_id": job_id}
        core._atomic_json(self.root / "jobs" / f"{job_id}.json", {"id": job_id, "phase": "running"})
        with (self.root / ("job-" + job_id + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(core._pending_launch_needs_recovery(plan))
        self.assertTrue(core._pending_launch_needs_recovery(plan))
        self.assertTrue(core._pending_launch_needs_recovery({"plan_id": "p" * 24}))

    def test_worker_lock_distinguishes_running_from_reused_pid(self):
        job_id = "a" * 24
        core._atomic_json(self.root / "jobs" / (job_id + ".json"),
                          {"id": job_id, "phase": "running", "pid": os.getpid()})
        self.assertEqual(jobs.jobs()[0]["phase"], "interrupted")
        with (self.root / ("job-" + job_id + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(jobs.jobs()[0]["phase"], "running")

    def test_async_operation_resumes_polling_without_second_submission(self):
        with patch.object(core, "run_cli", side_effect=[{"id": "operation-1"}, core.NebiusError("network disconnected")]):
            with self.assertRaises(core.NebiusError):
                core._compute_mutation("instance", "stop", "computeinstance-a")
        with patch.object(core, "run_cli", return_value={"status": {}}) as cli:
            core._compute_mutation("instance", "stop", "computeinstance-a")
            self.assertEqual(cli.call_args.args[0], ["compute", "instance", "operation", "get", "operation-1", "--format", "json"])
            self.assertEqual(cli.call_count, 1)
        self.assertFalse(core._cloud_operation_path("computeinstance-a").exists())

    def test_uncertain_submission_never_replays_delete(self):
        with patch.object(core, "run_cli", side_effect=core.NebiusError("timeout")):
            with self.assertRaises(core.NebiusError):
                core._compute_mutation("instance", "delete", "computeinstance-a")
        with patch.object(core, "run_cli", return_value={"status": {"state": "RUNNING"}}) as cli:
            with self.assertRaisesRegex(core.NebiusError, "unknown"):
                core._compute_mutation("instance", "delete", "computeinstance-a")
            self.assertEqual(cli.call_args.args[0][:3], ["compute", "instance", "get"])

    def test_completed_delete_can_resume_after_resource_disappears(self):
        with patch.object(core, "run_cli", side_effect=[{"id": "operation-1"}, {"status": {}}]):
            core._compute_mutation("instance", "delete", "computeinstance-a")
        with patch.object(core, "run_cli") as cli:
            core._compute_mutation("instance", "delete", "computeinstance-a")
            cli.assert_not_called()

    def test_per_job_progress_is_not_replaced_by_another_job(self):
        for index in ("a", "b"):
            with patch.dict(os.environ, {"NEBIUS_JOB_ID": index * 24}):
                core._write_operation("running", "delete", index)
        self.assertEqual(core._read_json(self.root / "jobs" / ("a" * 24 + ".operation.json"), {})["message"], "a")

    def test_add_pause_resume_and_remove(self):
        with patch.object(core, "_accessible_vm", return_value=({}, self.vm)), patch.object(ports, "install"), \
             patch.object(ports, "available", return_value=True):
            item = ports.add(self.vm["id"], 8188)
            self.assertEqual(item["local_port"], 8188)
            self.assertTrue(ports.mappings()[0]["enabled"])
            with self.assertRaisesRegex(core.NebiusError, "in use"):
                ports.add(self.vm["id"], 9000, 8188)
            ports.change(item["id"], "pause")
            self.assertEqual(ports.listing()[0]["state"], "Paused")
            ports.change(item["id"], "resume")
            ports.change(item["id"], "remove")
            self.assertEqual(ports.mappings(), [])

    def test_remove_vm_forgets_only_its_mappings_status_and_logs(self):
        mappings = [
            {"id": "target-a", "vm_id": self.vm["id"], "enabled": True, "local_port": 8188},
            {"id": "other", "vm_id": "computeinstance-other", "enabled": True, "local_port": 8000},
            {"id": "target-b", "vm_id": self.vm["id"], "enabled": False, "local_port": 7860},
        ]
        core._atomic_json(self.root / "ports.json", mappings)
        core._atomic_json(self.root / "ports-status.json", {
            "updated_at": time.time(),
            "ports": {item["id"]: {"state": "Connected"} for item in mappings},
        })
        runtime = self.root / "ports-runtime"
        runtime.mkdir()
        for item in mappings:
            (runtime / (item["id"] + ".log")).write_text(item["id"])

        self.assertEqual(ports.remove_vm(self.vm["id"]), 2)
        self.assertEqual([item["id"] for item in ports.mappings()], ["other"])
        self.assertEqual(list(core._read_json(self.root / "ports-status.json", {})["ports"]), ["other"])
        self.assertFalse((runtime / "target-a.log").exists())
        self.assertFalse((runtime / "target-b.log").exists())
        self.assertTrue((runtime / "other.log").exists())
        self.assertEqual(ports.remove_vm(self.vm["id"]), 0)

    def test_occupied_local_port_is_rejected_before_service_install(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            value = listener.getsockname()[1]
            with patch.object(core, "_accessible_vm", return_value=({}, self.vm)), patch.object(ports, "install") as install:
                with self.assertRaisesRegex(core.NebiusError, "in use"):
                    ports.add(self.vm["id"], 8000, value)
                install.assert_not_called()

    def test_forward_command_never_listens_publicly_or_disables_host_verification(self):
        item = {"vm_id": self.vm["id"], "username": "dev", "managed": True, "local_port": 18000, "remote_port": 8000}
        command = ports.ssh_command(item, "192.0.2.1", self.root / "socket")
        self.assertEqual(command[command.index("-L") + 1], "127.0.0.1:18000:127.0.0.1:8000")
        for setting in ("ExitOnForwardFailure=yes", "BatchMode=yes", "ServerAliveInterval=15", "ServerAliveCountMax=3", "StrictHostKeyChecking=accept-new"):
            self.assertIn(setting, command)
        self.assertNotIn("-f", command)
        with self.assertRaises(ValueError):
            ports.ssh_command(item, "--proxy-command=bad", self.root / "socket")

    def test_stale_status_does_not_claim_connected(self):
        core._atomic_json(self.root / "ports.json", [{"id": "a", "enabled": True, "local_port": 8000}])
        core._atomic_json(self.root / "ports-status.json", {"updated_at": 1, "ports": {"a": {"state": "Connected"}}})
        self.assertEqual(ports.listing()[0]["state"], "Waiting for local service")

    def test_ports_and_activity_fit_narrow_terminal(self):
        mapping = {"id": "a", "vm_id": self.vm["id"], "vm_name": "inference", "enabled": True,
                   "local_port": 18000, "remote_port": 8000, "state": "Reconnecting", "url": "http://127.0.0.1:18000"}
        for width, height in [(48, 20), (80, 24), (120, 44)]:
            application, screen = app(["\x1b"], width, height)
            with patch.object(ports, "listing", return_value=[mapping]), self.assertRaises(ui.Back):
                application.ports()
            self.assertIn("Reconnecting", screen.frames[-1])
            application, screen = app(["\x1b"], width, height)
            with patch.object(jobs, "jobs", return_value=[{"id": "a", "command": "delete", "phase": "running",
                                                         "title": "Delete VM · inference", "operation": {"message": "Deleting VM"}}]), self.assertRaises(ui.Back):
                application.activity()
            self.assertIn("Delete VM", screen.frames[-1])

    def test_lifecycle_ui_returns_immediately(self):
        application, _ = app([])
        with patch.object(jobs, "submit", return_value={"job_id": "a"}) as submit, patch.object(application, "watch") as watch:
            with self.assertRaises(ui.Background):
                application.mutate("Delete A", "delete", "--vm-id", "computeinstance-a", "--confirmed")
            submit.assert_called_once()
            watch.assert_not_called()

    def test_security_group_contains_only_ssh_ingress_and_outbound(self):
        plan = {"subnet_id": "vpcsubnet-a", "project": {"project_id": "project-a"}}
        with patch.object(core, "run_cli", side_effect=[{"spec": {"network_id": "vpcnetwork-a"}}, {},
                                                       {"metadata": {"id": "vpcsecuritygroup-a"}}, {}, {}]) as cli:
            self.assertEqual(core._ssh_security_group(plan), "vpcsecuritygroup-a")
        rules = [json.loads(c.args[0][3])["spec"] for c in cli.call_args_list if c.args[0][:3] == ["vpc", "security-rule", "create"]]
        ingress = [r for r in rules if "ingress" in r]
        self.assertEqual(len(ingress), 1)
        self.assertEqual(ingress[0]["ingress"]["destination_ports"], [22])
        self.assertEqual(ingress[0]["protocol"], "TCP")
        self.assertTrue(any("egress" in r for r in rules))

    def test_security_group_failure_never_returns_permissive_fallback(self):
        with patch.object(core, "run_cli", side_effect=core.NebiusError("permission denied")):
            with self.assertRaises(core.NebiusError):
                core._ssh_security_group({"subnet_id": "vpcsubnet-a"})

    def test_systemd_unit_is_enabled_at_login_and_supervises_ssh_children(self):
        target = self.root / "units/nebius-ports.service"
        with patch.object(ports, "unit_path", return_value=target), patch.object(ports.shutil, "which", return_value="/usr/bin/systemctl"), \
             patch.object(ports.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            ports.install()
        content = target.read_text()
        self.assertIn("WantedBy=default.target", content)
        self.assertIn("Restart=always", content)
        self.assertIn("KillMode=control-group", content)
        self.assertIn("nebius_ports.py", content)
        self.assertIn(["systemctl", "--user", "enable", "--now", "nebius-ports.service"], [c.args[0] for c in run.call_args_list])

    def test_forward_service_stops_child_when_mapping_removed(self):
        item = {"id": "abcdef", "vm_id": self.vm["id"], "vm_name": "test", "username": "dev",
                "address": "192.0.2.1", "enabled": True, "local_port": 18000, "remote_port": 8000}
        child = Mock()
        child.poll.return_value = None
        handlers = {}
        ticks = []
        def sleep(_):
            ticks.append(1)
            if len(ticks) == 2:
                handlers[ports.signal.SIGTERM]()
        future = Mock()
        future.done.return_value = False
        pool = Mock()
        pool.submit.return_value = future
        with patch.object(ports, "mappings", side_effect=[[item], []]), \
             patch.object(ports.signal, "signal", side_effect=lambda sig, handler: handlers.update({sig: handler})), \
             patch.object(ports.time, "sleep", side_effect=sleep), patch.object(ports, "available", return_value=True), \
             patch("concurrent.futures.ThreadPoolExecutor", return_value=pool), \
             patch.object(ports.subprocess, "Popen", return_value=child), \
             patch.object(ports.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            ports.serve()
        child.terminate.assert_called_once()
        child.wait.assert_called_once()

    def test_group_with_extra_ingress_is_never_reused(self):
        plan = {"subnet_id": "vpcsubnet-a", "project": {"project_id": "project-a"}}
        group = {"metadata": {"id": "vpcsecuritygroup-old", "labels": {"managed-by": core.MANAGED_BY}},
                 "spec": {"network_id": "vpcnetwork-a"}}
        with patch.object(core, "run_cli", side_effect=[{"spec": {"network_id": "vpcnetwork-a"}}, {"items": [group]},
                                                       {"items": [{"spec": {"ingress": {}, "access": "ALLOW"}}]},
                                                       {"metadata": {"id": "vpcsecuritygroup-new"}}, {}, {}]):
            self.assertEqual(core._ssh_security_group(plan), "vpcsecuritygroup-new")


if __name__ == "__main__":
    unittest.main()
