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
        self.command = self.root / ".local/bin/nebius"
        self.launcher = self.root / "plugin/bin/nebius-cli"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("#!/bin/sh\nexec \"$CLI\" \"$@\"\n")
        self.launcher.chmod(0o700)
        self.receipt = self.root / "install-receipt.json"
        self.receipt.write_text(json.dumps({"owned": {"cli": False}}))
        self.binary = self.root / "download"
        self.binary.write_text("#!/bin/sh\nprintf '%s\\n' '" + runtime.CLI_VERSION + "'\n")
        self.digest = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        self.clean_path = os.pathsep.join(
            directory for directory in os.environ["PATH"].split(os.pathsep)
            if not os.access(Path(directory) / "nebius", os.X_OK)
        )

    def install_stage(self, current=None, digest=None, path=None):
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
            "HOME": str(self.root),
            "CLI": str(current or self.legacy), "PRIVATE_CLI": str(self.private),
            "CLI_COMMAND": str(self.command), "CLI_LAUNCHER": str(self.launcher),
            "CLI_VERSION": runtime.CLI_VERSION, "CLI_BASE_URL": "https://example.invalid/never-contacted",
            "CLI_SHA256_X86_64": digest or self.digest, "CLI_SHA256_ARM64": digest or self.digest,
            "NEBIUS_TEST_BINARY": str(self.binary), "STATE_DIR": str(self.root),
            "INSTALL_RECEIPT": str(self.receipt), "TMPDIR": str(self.root),
            "PATH": path or str(self.command.parent) + os.pathsep + self.clean_path},
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
        self.assertTrue(self.command.is_symlink())
        self.assertEqual(os.readlink(self.command), str(self.launcher))
        self.assertTrue(receipt["owned"]["cli_link"])
        self.assertEqual(receipt["cli_link_path"], str(self.command))
        self.assertEqual(receipt["cli_link_target"], str(self.launcher))

    def test_upgrade_retains_previous_ownership_for_uninstall(self):
        self.legacy.write_text("#!/bin/sh\necho previous-version\n")
        self.legacy.chmod(0o700)
        self.receipt.write_text(json.dumps({"owned": {"cli": True}, "cli_path": str(self.legacy)}))
        result = self.install_stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["cli_path"], str(self.private))
        self.assertEqual(receipt["cli_previous_paths"], [str(self.legacy)])
        self.assertTrue(self.legacy.exists())
        self.assertEqual(self.install_stage(current=self.private).returncode, 0)
        self.assertEqual(json.loads(self.receipt.read_text())["cli_previous_paths"], [str(self.legacy)])

    def test_upgrade_does_not_claim_preexisting_cli(self):
        self.legacy.write_text("#!/bin/sh\necho previous-version\n")
        self.legacy.chmod(0o700)
        self.receipt.write_text(json.dumps({"owned": {"cli": False}, "cli_path": str(self.legacy)}))
        result = self.install_stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("cli_previous_paths", json.loads(self.receipt.read_text()))

    def test_uninstall_previous_versions_requires_known_path_and_checksum(self):
        source = (ROOT / "bin/nebius-uninstall").read_text()
        collect = 'previous_owned_clis=()' + source.split('previous_owned_clis=()', 1)[1].split('\nowned_cli_link=', 1)[0]
        remove = 'if [[ $cli_policy == "remove"' + source.split('if [[ $cli_policy == "remove"', 1)[1].split('\nstage="removing the Omarchy plugin"', 1)[0]
        previous = self.root / "old-private/nebius"
        previous.parent.mkdir()
        unknown = self.root / "unrelated-nebius"
        unknown.write_bytes(self.binary.read_bytes())
        self.receipt.write_text(json.dumps({"cli_previous_paths": [str(previous), str(unknown)]}))
        for case in ("verified", "modified", "symlink", "keep"):
            with self.subTest(case=case):
                previous.parent.mkdir(exist_ok=True)
                if previous.exists() or previous.is_symlink():
                    previous.unlink()
                if case == "symlink":
                    previous.symlink_to(unknown)
                else:
                    previous.write_bytes(b"changed" if case == "modified" else self.binary.read_bytes())
                result = subprocess.run(["bash", "-euc", collect + '\n' + remove], env={**os.environ,
                    "HOME": str(self.root), "RECEIPT": str(self.receipt),
                    "PREVIOUS_PRIVATE_CLI": str(previous), "owned_cli": str(self.private),
                    "cli_policy": "keep" if case == "keep" else "remove", "cli_owned": "true",
                    "CLI_SHA256": self.digest, "CLI_SHA256_ARM64": self.digest},
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(previous.exists(), case != "verified")
                self.assertTrue(unknown.exists())

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

    def test_existing_terminal_command_is_preserved(self):
        existing_bin = self.root / "existing-bin"
        existing_bin.mkdir()
        existing_command = existing_bin / "nebius"
        existing_command.write_text("#!/bin/sh\necho existing\n")
        existing_command.chmod(0o700)
        result = self.install_stage(path=str(existing_bin) + os.pathsep + self.clean_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.command.exists())
        receipt = json.loads(self.receipt.read_text())
        self.assertFalse(receipt["owned"].get("cli_link", False))
        self.assertIn(f"Existing terminal command preserved: {existing_command}", result.stdout)

    def test_existing_terminal_path_is_preserved_without_claiming_it(self):
        self.command.parent.mkdir(parents=True)
        self.command.write_text("user command")
        result = self.install_stage(path=self.clean_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.command.read_text(), "user command")
        self.assertFalse(json.loads(self.receipt.read_text())["owned"].get("cli_link", False))

    def test_matching_unowned_link_is_reused_without_claiming_it(self):
        self.command.parent.mkdir(parents=True)
        self.command.symlink_to(self.launcher)
        result = self.install_stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(self.command), str(self.launcher))
        self.assertFalse(json.loads(self.receipt.read_text())["owned"].get("cli_link", False))

    def test_terminal_launcher_executes_the_private_cli(self):
        data_home = self.root / "data"
        private = data_home / "nebius/cli" / runtime.CLI_VERSION / "nebius"
        private.parent.mkdir(parents=True)
        private.write_text("#!/bin/sh\nprintf 'cli:%s\\n' \"$*\"\n")
        private.chmod(0o700)
        command = self.root / ".local/bin/nebius"
        command.parent.mkdir(parents=True)
        command.symlink_to(ROOT / "bin/nebius-cli")
        result = subprocess.run(
            [str(command), "version"],
            env={**os.environ, "HOME": str(self.root), "XDG_DATA_HOME": str(data_home)},
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "cli:version\n")

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
