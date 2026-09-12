"""Actual installer stages with isolated files and a synthetic download."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libexec"))
import nebius_runtime as runtime
import nebius_agent_mcp as agent


class ReleaseSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.legacy = self.root / "existing-nebius"
        self.private = self.root / "private/cli/nebius"
        self.receipt = self.root / "install-receipt.json"
        self.receipt.write_text(json.dumps({"owned": {"cli": False}}))
        self.binary = self.root / "download"
        self.binary.write_text("#!/bin/sh\nprintf '%s\\n' '" + runtime.CLI_VERSION + "'\n")
        self.digest = hashlib.sha256(self.binary.read_bytes()).hexdigest()

    def install_stage(self, current=None, digest=None):
        source = (ROOT / "bin/nebius-setup").read_text()
        stage = source.split('stage="cli"\n', 1)[1].split('\nstage="account"', 1)[0]
        harness = """set -Eeuo pipefail
stage=cli
write_state() { :; }
say() { printf '%s\\n' "$1"; }
curl() {
  while (( $# )); do
    if [[ $1 == -o ]]; then cp "$NEBIUS_TEST_BINARY" "$2"; return; fi
    shift
  done
  return 1
}
""" + stage
        return subprocess.run(["bash", "-c", harness], env={**os.environ,
            "CLI": str(current or self.legacy), "PRIVATE_CLI": str(self.private),
            "CLI_VERSION": runtime.CLI_VERSION, "CLI_BASE_URL": "https://example.invalid/never-contacted",
            "CLI_SHA256_X86_64": digest or self.digest, "CLI_SHA256_ARM64": digest or self.digest,
            "NEBIUS_TEST_BINARY": str(self.binary), "STATE_DIR": str(self.root),
            "INSTALL_RECEIPT": str(self.receipt), "TMPDIR": str(self.root)},
            text=True, capture_output=True, timeout=10)

    def test_different_user_cli_is_preserved_and_private_copy_owned(self):
        original = "#!/bin/sh\necho user-version\n"
        self.legacy.write_text(original)
        self.legacy.chmod(0o700)
        result = self.install_stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.legacy.read_text(), original)
        self.assertEqual(self.private.read_bytes(), self.binary.read_bytes())
        receipt = json.loads(self.receipt.read_text())
        self.assertTrue(receipt["owned"]["cli"])
        self.assertEqual(receipt["cli_path"], str(self.private))
        self.assertTrue(os.access(self.private, os.X_OK))

    def test_matching_user_cli_is_reused_without_claiming_ownership(self):
        self.legacy.write_bytes(self.binary.read_bytes())
        self.legacy.chmod(0o700)
        result = self.install_stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.private.exists())
        self.assertFalse(json.loads(self.receipt.read_text())["owned"]["cli"])

    def test_fresh_install_and_repair_reuse_the_same_private_cli(self):
        result = self.install_stage(current=self.private)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = self.receipt.read_text()
        result = self.install_stage(current=self.private)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.receipt.read_text(), receipt)

    def test_bad_download_never_installs_or_changes_ownership(self):
        result = self.install_stage(digest="0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.private.exists())
        self.assertFalse(json.loads(self.receipt.read_text())["owned"]["cli"])

    def test_unexpected_private_file_or_symlink_is_never_replaced(self):
        self.private.parent.mkdir(parents=True)
        self.private.write_text("keep this")
        result = self.install_stage(current=self.private)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.private.read_text(), "keep this")
        self.private.unlink()
        self.private.symlink_to(self.root / "missing-target")
        result = self.install_stage(current=self.private)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.private.is_symlink())
        self.assertFalse(self.private.exists())

    def test_uninstall_removes_only_verified_owned_private_binary(self):
        self.assertEqual(self.install_stage().returncode, 0)
        self.legacy.write_text("user CLI must remain")
        source = (ROOT / "bin/nebius-uninstall").read_text()
        stage = 'if [[ $cli_policy == "remove"' + source.split('if [[ $cli_policy == "remove"', 1)[1].split('\nstage="removing the Omarchy plugin"', 1)[0]
        result = subprocess.run(["bash", "-ec", stage], env={**os.environ,
            "cli_policy": "remove", "cli_owned": "true", "owned_cli": str(self.private),
            "CLI_SHA256": self.digest, "CLI_SHA256_ARM64": self.digest}, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.private.exists())
        self.assertEqual(self.legacy.read_text(), "user CLI must remain")

    def test_runtime_prefers_private_copy_without_changing_legacy(self):
        with patch.object(runtime.Path, "home", return_value=self.root), \
             patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "data")}, clear=True):
            legacy = self.root / ".nebius/bin/nebius"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("existing")
            self.assertEqual(runtime.cli_path(), legacy)
            private = runtime.private_cli_path()
            private.parent.mkdir(parents=True)
            private.write_text("private")
            self.assertEqual(runtime.cli_path(), private)
            self.assertEqual(legacy.read_text(), "existing")

    def test_mcp_and_manifest_share_release_version(self):
        self.assertEqual(agent.SERVER_INFO["version"], json.loads((ROOT / "manifest.json").read_text())["version"])


if __name__ == "__main__":
    unittest.main()
