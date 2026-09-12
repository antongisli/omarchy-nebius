"""First-login races and keyboard-first lifecycle navigation, without cloud writes."""
import curses
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import nebius_ssh as ssh
import nebius_ui as ui
from test_ui import app


VM = {"id": "computeinstance-test", "name": "training", "state": "running", "region": "eu-north1",
      "allocation": "on_demand", "project_name": "personal", "can_delete": True, "managed": True,
      "ssh_user": "dev", "disk_id": "computedisk-test", "platform": "gpu-h100-sxm"}


class SSHReadinessTests(unittest.TestCase):
    def setUp(self):
        self.connection = {"managed": True, "command": ["ssh", "-o", "StrictHostKeyChecking=accept-new",
                           "-o", "HostKeyAlias=computeinstance-test", "-i", "/key", "dev@192.0.2.1"]}
        self.clock = 0

    def sleep(self, seconds):
        self.clock += seconds

    def run_probe(self, outcomes, **options):
        results = iter(outcomes)
        def launch(command, **kwargs):
            code, error = next(results)
            kwargs["stderr"].write(error)
            return Mock(returncode=code, poll=lambda: code)
        with patch.object(ssh.time, "monotonic", side_effect=lambda: self.clock), \
             patch.object(ssh.time, "sleep", side_effect=self.sleep), \
             patch.object(ssh.subprocess, "Popen", side_effect=launch) as process:
            result = ssh.wait_ready(self.connection, **options)
        return result, process

    def test_first_connection_retries_boot_and_key_installation(self):
        progress = Mock()
        result, process = self.run_probe([(255, "Connection refused"), (255, "Permission denied (publickey)."), (0, "")], progress=progress)
        self.assertTrue(result)
        self.assertEqual(process.call_count, 3)
        self.assertGreaterEqual(self.clock, 4)
        self.assertTrue(any("SSH key" in call.args[0] for call in progress.call_args_list))

    def test_probe_preserves_identity_and_host_key_checks(self):
        command = ssh.probe_command(self.connection)
        self.assertEqual(command[-2:], ["dev@192.0.2.1", "true"])
        for value in ("BatchMode=yes", "HostKeyAlias=computeinstance-test", "StrictHostKeyChecking=accept-new", "/key"):
            self.assertIn(value, command)
        self.assertNotIn("StrictHostKeyChecking=no", command)

    def test_changed_host_key_is_not_retried(self):
        with self.assertRaisesRegex(ssh.SSHError, "IDENTIFICATION HAS CHANGED"):
            self.run_probe([(255, "REMOTE HOST IDENTIFICATION HAS CHANGED!")])
        self.assertEqual(self.clock, 0)

    def test_existing_vm_still_requires_successful_authentication(self):
        self.connection["managed"] = False
        result, process = self.run_probe([(255, "Permission denied (publickey,password)."), (0, "")])
        self.assertTrue(result)
        self.assertEqual(process.call_count, 2)

    def test_timeout_has_actionable_error(self):
        with self.assertRaisesRegex(ssh.SSHError, "VM is unchanged"):
            self.run_probe([(255, "Connection timed out")] * 10, timeout=3)
        self.assertLess(self.clock, 4)

    def test_escape_terminates_only_probe(self):
        process = Mock(returncode=None)
        process.poll.side_effect = lambda: process.returncode
        process.terminate.side_effect = lambda: setattr(process, "returncode", -15)
        with patch.object(ssh.subprocess, "Popen", return_value=process):
            with self.assertRaises(ssh.SSHCancelled):
                ssh.wait_ready(self.connection, cancelled=Mock(side_effect=[False, True]))
        process.terminate.assert_called_once()


class InteractionTests(unittest.TestCase):
    def test_every_action_row_has_a_working_shortcut(self):
        rows = [("SSH", "", "connect"), ("Stop", "", "stop"), ("Ports", "", "ports"),
                ("Connection", "", "settings"), ("Storage", "", "storage"), ("Details", "", "details"), ("Delete", "", "delete")]
        for key, value in zip("cspeoid", [row[2] for row in rows]):
            application, screen = app([key])
            self.assertEqual(application.menu("Actions", rows), value)
            self.assertIn("[" + key.upper() + "]", "\n".join(screen.frames))

    def test_resource_number_selects_without_interfering_with_search(self):
        rows = [("One", "", {"id": "one"}), ("Two", "", {"id": "two"})]
        application, _ = app(["2"])
        self.assertEqual(application.menu("VMs", rows), {"id": "two"})
        application, _ = app(["/", "2", "\n", "\x1b"])
        with self.assertRaises(ui.Back):
            application.menu("VMs", rows)

    def test_home_ports_never_shows_allocation_switch(self):
        application, screen = app(["p"])
        self.assertEqual(application.menu("Home", [("SSH port forwarding", "", "ports")], actions={"p": "ports"}), "ports")
        self.assertNotIn("Preemptible", screen.frames[-1])
        self.assertIn("[P]", screen.frames[-1])

    def test_escape_and_b_background_without_stopping_worker(self):
        for key in ("\x1b", "b", "B", 27):
            application, screen = app([key])
            with patch.object(ui.core, "_read_json", return_value={"phase": "running"}), \
                 patch.object(ui.os, "kill") as kill:
                with self.assertRaises(ui.Background):
                    application.watch("a" * 24, "Creating VM")
            kill.assert_not_called()
            self.assertIn("[Esc/B]", screen.frames[-1])

    def test_delete_uses_one_review_and_explicit_d_without_arrow_navigation(self):
        application, screen = app(["d", "d"], 120, 44)
        with patch.object(application, "mutate") as mutate:
            application.vm_actions(dict(VM))
        mutate.assert_called_once_with("Delete VM and boot disk · training", "delete", "--vm-id", VM["id"], "--confirmed", "--expected-disk-id", VM["disk_id"])
        self.assertEqual(len(screen.frames), 2)
        self.assertIn("Cannot be undone", screen.frames[-1])
        self.assertIn("Secondary disks", screen.frames[-1])

    def test_d_cannot_skip_unread_delete_terms(self):
        application, screen = app(["d", "\x1b"], 48, 20)
        self.assertFalse(application.confirm_launch(["Data loss warning " * 40], "{}", confirm_key="d", action="delete permanently"))
        self.assertIn("read next page", screen.frames[0])

    def test_overview_p_opens_ports_for_highlighted_vm(self):
        application, screen = app(["j", "p", "\x1b"])
        second = {**VM, "id": "computeinstance-second", "name": "second"}
        application.inventory = {"vms": [VM, second]}
        with patch.object(application, "ports") as ports, patch.object(application, "active_job", return_value=None), \
             patch.object(ui.inventory_view.Poller, "poll", side_effect=lambda snapshot, entries, **kwargs: snapshot), \
             patch.object(application, "read", return_value=application.inventory):
            application.overview()
        ports.assert_called_once_with(second)
        self.assertIn("› second", screen.frames[-1])
        self.assertIn("[P] ports", screen.frames[-1])

    def test_protected_vm_direct_delete_cannot_submit(self):
        application, _ = app(["\n"])
        with patch.object(application, "mutate") as mutate:
            application.vm_actions({**VM, "can_delete": False}, action="delete")
        mutate.assert_not_called()

    def test_ssh_failure_stays_visible_and_saves_diagnostic(self):
        application, screen = app(["v"])
        with tempfile.TemporaryDirectory() as state, patch.object(ui.core, "STATE_DIR", Path(state)), \
             patch.object(application, "read", return_value={"name": "training"}), \
             patch.object(ui.ssh_client, "wait_ready", side_effect=ssh.SSHError("Connection timed out")):
            application.ssh(dict(VM))
            saved = ui.core._read_json(Path(state) / "ssh-last-error.json", {})
        self.assertEqual(saved["error"], "Connection timed out")
        self.assertIn("SSH did not connect", screen.frames[-1])
        self.assertIn("[R]", screen.frames[-1])

    def test_agent_ssh_launch_uses_the_same_checked_terminal(self):
        with tempfile.TemporaryDirectory() as state, patch.object(ui.core, "CONNECTIONS_FILE", Path(state) / "connections.json"), \
             patch.object(ui.core, "_accessible_vm", return_value=({}, {**VM, "public_ip": "192.0.2.1"})), \
             patch.object(ui.core.subprocess, "Popen") as launch:
            ui.core.connect_vm(VM["id"])
        command = launch.call_args.args[0]
        self.assertIn("connect", command)
        self.assertIn("--vm-id", command)
        self.assertTrue(any(part.endswith("/bin/nebius-ui") for part in command))


if __name__ == "__main__":
    unittest.main()
