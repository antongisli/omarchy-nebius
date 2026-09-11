"""Read-only widget counts and plugin migration safety."""

import datetime as dt
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import nebius_core as core


class WidgetCopyTests(unittest.TestCase):
    def test_expired_session_is_prominent_and_actionable(self):
        widget = (Path(__file__).resolve().parents[1] / "qml/v057/Widget.qml").read_text()
        self.assertIn("readonly property bool needsReconnect:", widget)
        self.assertIn('"Nebius session expired — reconnect required.', widget)
        self.assertIn('"RECONNECT"', widget)
        self.assertIn("Press S or Enter to reconnect.", widget)


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
