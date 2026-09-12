"""Subprocess tests for the persistent worker, using a synthetic local CLI."""

import json
import hashlib
import fcntl
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkerTests(unittest.TestCase):
    def test_rejected_worker_does_not_overwrite_active_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "nebius"
            state.mkdir()
            previous = {"phase": "running", "stage": "instance", "disk_id": "computedisk-kept", "vm_id": "computeinstance-kept"}
            (state / "operation.json").write_text(json.dumps(previous))
            with (state / ("mutation-" + hashlib.sha256(b"computeinstance-test").hexdigest() + ".lock")).open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                job_id = secrets.token_hex(12)
                result = subprocess.run([sys.executable, str(ROOT / "libexec/nebius_job.py"), job_id,
                                         "stop", "--vm-id", "computeinstance-test"],
                                        env={**os.environ, "XDG_STATE_HOME": temporary}, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads((state / "operation.json").read_text()), previous)
            self.assertEqual(json.loads((state / "jobs" / f"{job_id}.json").read_text())["phase"], "error")

    def run_worker(self, scenario):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        state = root / "nebius"
        state.mkdir()
        (state / "capacity.json").write_text(json.dumps({
            "schema": "nebius.omarchy-capacity/v3", "offerings": [{"region": "eu-west1"}]
        }))
        # Isolate both the executable and the worker's credential-expiry check.
        fake = root / "fake-nebius"
        fake.write_bytes((ROOT / "tests/fake_nebius.py").read_bytes())
        fake.chmod(0o700)
        env = {**os.environ, "XDG_STATE_HOME": str(root), "NEBIUS_CLI_BIN": str(fake),
               "NEBIUS_TEST_STATE": str(root), "NEBIUS_TEST_SCENARIO": scenario}
        job_id = secrets.token_hex(12)
        runner = (
            "import os, runpy, sys; from pathlib import Path; "
            "sys.argv = sys.argv[1:]; "
            "sys.path.insert(0, str(Path(sys.argv[0]).parent)); "
            "import nebius_core; "
            "nebius_core.CREDENTIALS_FILE = Path(os.environ['NEBIUS_TEST_STATE']) / 'credentials.yaml'; "
            "runpy.run_path(sys.argv[0], run_name='__main__')"
        )
        result = subprocess.run([sys.executable, "-c", runner, str(ROOT / "libexec/nebius_job.py"), job_id,
                                 "create-project", "--region", "eu-west1", "--name", "my-training", "--confirmed"],
                                env=env, capture_output=True, text=True, timeout=10)
        job = json.loads((state / "jobs" / f"{job_id}.json").read_text())
        operation = json.loads((state / "operation.json").read_text())
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
        return result, job, operation, calls, state

    def test_zero_quota_never_submits_project(self):
        result, job, operation, calls, state = self.run_worker("quota-zero")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(job["phase"], "error")
        self.assertIn("SSD storage", operation["message"])
        self.assertFalse(any("create" in command for command in calls))

    def test_network_failure_reports_project_as_created(self):
        result, job, operation, calls, state = self.run_worker("network-error")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(operation["project_id"], "project-created")
        self.assertIn("already exists", operation["recovery"])
        projects = json.loads((state / "projects.json").read_text())
        self.assertEqual(projects["projects"][0]["project_name"], "my-training")

    def test_success_has_durable_result_and_user_selected_name(self):
        result, job, operation, calls, state = self.run_worker("ready")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(job["result"]["project_name"], "my-training")
        self.assertEqual(job["result"]["subnet_id"], "vpcsubnet-created")
        self.assertEqual(operation["phase"], "ready")
        self.assertEqual(job["operation"], operation)
        self.assertTrue((state / "activity.log").exists())


if __name__ == "__main__":
    unittest.main()
