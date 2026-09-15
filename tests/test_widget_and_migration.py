"""Read-only widget counts and plugin migration safety."""

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import nebius_core as core

ROOT = Path(__file__).resolve().parents[1]


class SetupCompletionTests(unittest.TestCase):
    def test_status_distinguishes_new_install_from_completed_setup(self):
        for saved, completed in [(None, False), ({"phase": "running"}, False),
                                 ({"phase": "error"}, False), ({"phase": "ready"}, True),
                                 ({"phase": "error", "completed": True}, True)]:
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                state = home / ".state/nebius"
                state.mkdir(parents=True)
                if saved is not None:
                    (state / "setup.json").write_text(json.dumps(saved))
                result = subprocess.run(
                    [str(ROOT / "bin/nebius-status"), "--json"],
                    env={**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / ".state")},
                    text=True, capture_output=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                status = json.loads(result.stdout)
                self.assertFalse(status["ready"])
                self.assertEqual(status["setup_complete"], completed)

    def test_setup_remembers_completion_through_reconnect_and_repair(self):
        # Run just the production state writer, never installation or authentication.
        script = (ROOT / "bin/nebius-setup").read_text()
        body = script.split("write_state() {\n", 1)[1].split("\n}\n", 1)[0]
        command = "set -e\nwrite_state() {\n" + body + '\n}\nwrite_state "$1" test "Test state"'
        for previous, phase, expected in [(None, "running", False), (None, "ready", True),
                                          ({"phase": "ready"}, "running", True),
                                          ({"phase": "running", "completed": True}, "error", True)]:
            with self.subTest(previous=previous, phase=phase), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "setup.json"
                if previous is not None:
                    state.write_text(json.dumps(previous))
                result = subprocess.run(
                    ["bash", "-c", command, "setup-state-test", phase],
                    env={**os.environ, "STATE_DIR": directory, "SETUP_STATE": str(state)},
                    text=True, capture_output=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                saved = json.loads(state.read_text())
                self.assertEqual(saved["completed"], expected)
                self.assertEqual(saved["phase"], phase)


class WidgetCopyTests(unittest.TestCase):
    def test_expired_session_is_prominent_and_actionable(self):
        entrypoint = json.loads((ROOT / "manifest.json").read_text())["entryPoints"]["barWidget"]
        widget = (ROOT / entrypoint).read_text()
        self.assertIn("readonly property bool needsReconnect:", widget)
        self.assertIn('"Nebius session expired — reconnect required.', widget)
        self.assertIn('"RECONNECT"', widget)
        self.assertIn("Press S or Enter to reconnect.", widget)

    def test_settings_are_available_from_the_widget(self):
        entrypoint = json.loads((ROOT / "manifest.json").read_text())["entryPoints"]["barWidget"]
        widget = (ROOT / entrypoint).read_text()
        self.assertIn('{ key: "E", title: "Settings", screen: "settings" }', widget)
        self.assertIn('function settings(): string { root.launch("settings")', widget)


class CountTests(unittest.TestCase):
    def snapshot(self, **kwargs):
        return {"updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "vms": [], "errors": [], **kwargs}

    def test_counts_only_unique_running_vms_not_disks_or_pending(self):
        rows = [{"id": "computeinstance-one", "state": "running", "managed": True},
                {"id": "computeinstance-two", "state": "RUNNING", "managed": False},
                {"id": "computeinstance-one", "state": "running"},
                {"id": "computeinstance-stopped", "state": "stopped"},
                {"id": "computeinstance-gone", "state": "running", "instance_deleted": True},
                {"id": "computedisk-remaining", "state": "disk remains"}]
        with patch.object(core, "list_vms", return_value=self.snapshot(vms=rows, recovery=[{}], reusable_disks=[{}])) as listing:
            value = core.live_vm_count(force_refresh=True)
        self.assertEqual(value["count"], 2)
        self.assertEqual(value["state"], "current")
        listing.assert_called_once_with(force_refresh=True)

    def test_confirmed_empty_inventory_is_zero(self):
        with patch.object(core, "list_vms", return_value=self.snapshot()):
            self.assertEqual(core.live_vm_count()["count"], 0)

    def test_partial_stale_and_missing_inventory_never_claim_zero(self):
        for snapshot in [self.snapshot(errors=[{"error": "permission denied"}]),
                         self.snapshot(vms=[{"id": "one", "state": "running", "stale": True}]),
                         self.snapshot(updated_at="2000-01-01T00:00:00Z"),
                         self.snapshot(updated_at="invalid"), self.snapshot(vms=None)]:
            with self.subTest(snapshot=snapshot), patch.object(core, "list_vms", return_value=snapshot):
                self.assertIsNone(core.live_vm_count()["count"])

    def test_auth_and_network_errors_are_unknown_not_zero(self):
        for error in ["Nebius account session expired", "timed out"]:
            with patch.object(core, "list_vms", side_effect=core.NebiusError(error)):
                result = core.live_vm_count(force_refresh=True)
            self.assertIsNone(result["count"])
            self.assertIn(error, result["detail"])

if __name__ == "__main__":
    unittest.main()
