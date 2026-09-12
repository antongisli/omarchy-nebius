"""Exercise CLI stdout parsing, not only already-parsed run_cli mock values."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import nebius_core as core


OPERATION = "computeoperation-test123"


class AsyncResponseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        cli = self.root / "nebius"
        cli.touch(mode=0o700)
        for key, value in {"STATE_DIR": self.root, "OPERATION_FILE": self.root / "operation.json",
                           "CLI": cli, "CREDENTIALS_FILE": self.root / "credentials.yaml"}.items():
            context = patch.object(core, key, value)
            context.start()
            self.addCleanup(context.stop)

    def test_raw_async_stdout_is_saved_before_json_status_polling(self):
        for kind, action in (("instance", "start"), ("instance", "stop"),
                             ("instance", "delete"), ("disk", "delete")):
            for response in (OPERATION + "\n", json.dumps(OPERATION), json.dumps({"id": OPERATION})):
                resource = "compute" + kind + "-test"
                path = core._cloud_operation_path(resource)
                path.unlink(missing_ok=True)
                calls = []
                def output(command, **_):
                    arguments = command[command.index("compute"):]
                    calls.append(arguments)
                    if len(calls) == 1:
                        self.assertEqual(arguments, ["compute", kind, action, resource, "--async", "--format", "json"])
                        return response
                    self.assertEqual(arguments, ["compute", kind, "operation", "get", OPERATION, "--format", "json"])
                    saved = json.loads(path.read_text())
                    self.assertEqual(saved["operation_id"], OPERATION)
                    self.assertEqual(saved["phase"], "running")
                    return '{"status": {}}'
                with self.subTest(kind=kind, action=action, response=response), patch.object(core, "_run", side_effect=output):
                    core._compute_mutation(kind, action, resource)
                self.assertEqual(len(calls), 2)
                if action == "delete":
                    self.assertEqual(json.loads(path.read_text())["phase"], "done")
                else:
                    self.assertFalse(path.exists())

    def test_failed_poll_keeps_id_and_resume_never_resubmits(self):
        with patch.object(core, "_run", side_effect=[OPERATION, core.NebiusError("network disconnected")]):
            with self.assertRaisesRegex(core.NebiusError, "disconnected"):
                core._compute_mutation("instance", "stop", "computeinstance-test")
        with patch.object(core, "_run", return_value='{"status": {}}') as run:
            core._compute_mutation("instance", "stop", "computeinstance-test")
        run.assert_called_once()
        self.assertNotIn("--async", run.call_args.args[0])
        self.assertIn(OPERATION, run.call_args.args[0])

    def test_invalid_reply_preserves_ambiguity_and_does_not_scrape_ids(self):
        invalid = ("", "\n", "null", "true", "123", "{}", "[]", '{"id": null}',
                   "Success", "warning: " + OPERATION, OPERATION + "\n" + OPERATION,
                   '{"id": "--proxy=bad"}', '"computeoperation-test\\n"', OPERATION + "!")
        for response in invalid:
            path = core._cloud_operation_path("computeinstance-test")
            path.unlink(missing_ok=True)
            with self.subTest(response=response), patch.object(core, "_run", return_value=response) as run:
                with self.assertRaisesRegex(core.NebiusError, "no valid operation ID"):
                    core._compute_mutation("instance", "delete", "computeinstance-test")
                run.assert_called_once()
                self.assertEqual(json.loads(path.read_text())["phase"], "submitting")
            with patch.object(core, "_run", return_value='{"status": {"state": "RUNNING"}}') as run:
                with self.assertRaisesRegex(core.NebiusError, "outcome is unknown"):
                    core._compute_mutation("instance", "delete", "computeinstance-test")
                run.assert_called_once()
                self.assertNotIn("--async", run.call_args.args[0])

    def test_json_resources_and_operation_status_remain_strict(self):
        with patch.object(core, "_run", return_value=OPERATION):
            with self.assertRaisesRegex(core.NebiusError, "invalid JSON"):
                core.run_cli(["compute", "instance", "get", "computeinstance-test", "--format", "json"])
        with patch.object(core, "_run", side_effect=[OPERATION, "not JSON"]):
            with self.assertRaisesRegex(core.NebiusError, "invalid JSON"):
                core._compute_mutation("instance", "start", "computeinstance-test")
        self.assertEqual(core._read_json(core._cloud_operation_path("computeinstance-test"), {})["operation_id"], OPERATION)

    def test_existing_ambiguous_start_reconciles_read_only_when_running(self):
        path = core._cloud_operation_path("computeinstance-test")
        core._atomic_json(path, {"kind": "instance", "action": "start",
                                "resource_id": "computeinstance-test", "phase": "submitting"})
        with patch.object(core, "_run", return_value='{"status": {"state": "RUNNING"}}') as run:
            core._compute_mutation("instance", "start", "computeinstance-test")
        run.assert_called_once()
        self.assertNotIn("--async", run.call_args.args[0])
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
