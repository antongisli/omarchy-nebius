"""Reinstall-safe SSH identity selection without adopting or mutating VMs."""
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import nebius_core as core
import nebius_ports as ports
import nebius_ssh as ssh


VM_ID = "computeinstance-retained"
PROJECT = {"project_id": "project-test", "project_name": "Test", "region": "eu-north1"}
INSTANCE = {
    "metadata": {"id": VM_ID, "name": "retained-vm", "parent_id": PROJECT["project_id"],
                 "labels": {"managed-by": core.MANAGED_BY}},
    "spec": {"cloud_init_user_data": "#cloud-config\nusers:\n  - name: dev\n"},
    "status": {"state": "RUNNING", "network_interfaces": [
        {"public_ip_address": {"address": "192.0.2.1/32"}}]},
}


class SSHIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for key, path in {"STATE_DIR": self.root, "REGISTRY_FILE": self.root / "vms.json",
                          "CONNECTIONS_FILE": self.root / "connections.json",
                          "SSH_KEY": self.root / "nebius-key"}.items():
            context = patch.object(core, key, path)
            context.start()
            self.addCleanup(context.stop)
        core.SSH_KEY.write_text("synthetic test identity; never passed to a real SSH client")

    def connection(self, instance=INSTANCE):
        with patch.object(core, "run_cli", return_value=instance) as cli, \
             patch.object(core, "sync_personal_projects", return_value={"projects": [PROJECT]}), \
             patch.object(core, "ensure_ssh_key") as generate, \
             patch.object(core.subprocess, "Popen") as launch:
            connection = core.connect_vm(VM_ID, launch=False)
        cli.assert_called_once_with(["compute", "instance", "get", VM_ID, "--format", "json"], timeout=30)
        generate.assert_not_called()
        launch.assert_not_called()
        return connection

    def test_reinstall_retains_login_key_without_adoption_or_cloud_writes(self):
        summary = core._vm_summary(INSTANCE, PROJECT)
        self.assertEqual(summary["ssh_user"], "dev")
        self.assertEqual(summary["ssh_identity"], "nebius")
        self.assertFalse(summary["managed"])
        self.assertTrue(summary["can_delete"])
        connection = self.connection()
        command = connection["command"]
        self.assertEqual(command[command.index("-i") + 1], str(core.SSH_KEY))
        self.assertNotIn("IdentitiesOnly=yes", command)
        self.assertIn("StrictHostKeyChecking=accept-new", command)
        self.assertIn("HostKeyAlias=" + VM_ID, command)
        self.assertEqual(command[-1], "dev@192.0.2.1")
        self.assertFalse(connection["managed"])
        self.assertFalse(core.REGISTRY_FILE.exists())
        with self.assertRaisesRegex(core.NebiusError, "not created"):
            core._registered(VM_ID)
        with patch.object(core, "run_cli") as cli:
            with self.assertRaises(core.NebiusError):
                core.delete_vm(VM_ID, False)
            cli.assert_not_called()

    def test_probe_and_interactive_ssh_offer_the_same_key_and_host_policy(self):
        connection = self.connection()
        command = ssh.probe_command(connection)
        self.assertEqual(command[command.index("-i") + 1], str(core.SSH_KEY))
        self.assertIn("HostKeyAlias=" + VM_ID, command)
        self.assertIn("StrictHostKeyChecking=accept-new", command)
        self.assertEqual(command[-2:], ["dev@192.0.2.1", "true"])

    def test_unrelated_vms_do_not_receive_plugin_identity(self):
        for labels in ({}, None, {"managed-by": "other-tool"}, {"managed-by": "another-plugin"}):
            with self.subTest(labels=labels):
                instance = copy.deepcopy(INSTANCE)
                instance["metadata"]["labels"] = labels
                connection = self.connection(instance)
                self.assertNotIn("-i", connection["command"])
                self.assertNotIn("IdentitiesOnly=yes", connection["command"])

    def test_missing_retained_key_preserves_agent_without_creating_key(self):
        core.SSH_KEY.unlink()
        connection = self.connection()
        self.assertNotIn("-i", connection["command"])
        self.assertNotIn("IdentitiesOnly=yes", connection["command"])
        self.assertFalse(core.SSH_KEY.exists())

    def test_current_managed_vm_keeps_explicit_identity_policy(self):
        core._atomic_json(core.REGISTRY_FILE, {"vms": [{"id": VM_ID, "ssh_user": "dev"}]})
        connection = self.connection()
        self.assertTrue(connection["managed"])
        self.assertIn("IdentitiesOnly=yes", connection["command"])
        self.assertEqual(connection["command"][connection["command"].index("-i") + 1], str(core.SSH_KEY))

    def test_new_forward_saves_identity_without_management_ownership(self):
        vm = core._vm_summary(INSTANCE, PROJECT)
        with patch.object(core, "_accessible_vm", return_value=(INSTANCE, vm)), \
             patch.object(ports, "install"), patch.object(ports, "available", return_value=True):
            item = ports.add(VM_ID, 8000, 18000)
        self.assertEqual(ports.mappings()[0]["ssh_identity"], "nebius")
        self.assertFalse(item["managed"])
        command = ports.ssh_command(item, "192.0.2.1", self.root / "control")
        self.assertEqual(command[command.index("-i") + 1], str(core.SSH_KEY))
        self.assertNotIn("IdentitiesOnly=yes", command)
        self.assertIn("127.0.0.1:18000:127.0.0.1:8000", command)
        self.assertIn("StrictHostKeyChecking=accept-new", command)

    def test_legacy_forward_resolves_identity_from_live_metadata(self):
        item = {"id": "abcdef", "vm_id": VM_ID, "vm_name": "test", "username": "dev",
                "managed": False, "enabled": True, "local_port": 18000, "remote_port": 8000}
        child = Mock()
        child.poll.return_value = None
        handlers, ticks = {}, []
        def sleep(_):
            ticks.append(1)
            if len(ticks) == 2:
                handlers[ports.signal.SIGTERM]()
        future = Mock()
        future.done.return_value = True
        future.result.return_value = INSTANCE
        pool = Mock()
        pool.submit.return_value = future
        with patch.object(ports, "mappings", return_value=[item]), \
             patch.object(ports.signal, "signal", side_effect=lambda sig, handler: handlers.update({sig: handler})), \
             patch.object(ports.time, "sleep", side_effect=sleep), patch.object(ports, "available", return_value=True), \
             patch("concurrent.futures.ThreadPoolExecutor", return_value=pool), \
             patch.object(ports.subprocess, "Popen", return_value=child) as launch, \
             patch.object(ports.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            ports.serve()
        launch.assert_called_once()
        command = launch.call_args.args[0]
        self.assertEqual(command[command.index("-i") + 1], str(core.SSH_KEY))
        self.assertNotIn("IdentitiesOnly=yes", command)
        self.assertNotIn("ssh_identity", item)
        self.assertFalse(core.REGISTRY_FILE.exists())

    def test_unrelated_forward_uses_existing_keys_and_agent(self):
        item = {"vm_id": VM_ID, "username": "ubuntu", "managed": False,
                "local_port": 18000, "remote_port": 8000}
        command = ports.ssh_command(item, "192.0.2.1", self.root / "control")
        self.assertNotIn("-i", command)
        self.assertNotIn("IdentitiesOnly=yes", command)


if __name__ == "__main__":
    unittest.main()
