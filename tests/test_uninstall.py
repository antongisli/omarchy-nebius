"""Uninstaller boundary: local setup is removed; cloud state is untouched."""

from __future__ import annotations

import os
from pathlib import Path
import json
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
UNINSTALL = ROOT / "bin/nebius-uninstall"


class UninstallTests(unittest.TestCase):
    def run_uninstall(self, home: Path, *arguments: str, path: str | None = None):
        environment = {**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / ".state")}
        environment["XDG_CACHE_HOME"] = str(home / ".cache")
        if path:
            environment["PATH"] = path
        return subprocess.run([str(UNINSTALL), *arguments], text=True, capture_output=True, env=environment)

    def test_dry_run_warns_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            marker = home / ".state/nebius/keep"
            marker.parent.mkdir(parents=True)
            marker.write_text("local")
            result = self.run_uninstall(home, "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Uninstall Nebius plugin", result.stdout)
            self.assertIn("Cloud resources are left exactly as they are", result.stdout)
            self.assertIn("may continue to incur charges", result.stdout)
            self.assertTrue(marker.exists())

    def test_yes_removes_local_setup_but_not_cloud_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin = home / "fake-bin"
            fake_bin.mkdir()
            plugin = home / ".config/omarchy/plugins/nebius"
            (plugin / "libexec").mkdir(parents=True)
            (plugin / "libexec/nebius_agent_mcp.py").write_text("# test")
            state_dir = home / ".state/nebius"
            state_dir.mkdir(parents=True)
            (state_dir / "setup.log").write_text("setup")
            private_cache = home / ".cache/nebius/uv"
            private_cache.mkdir(parents=True)
            (private_cache / "environment").write_text("plugin runtime")
            other_cache = home / ".cache/unrelated"
            other_cache.write_text("keep")
            nebius_dir = home / ".nebius/bin"
            nebius_dir.mkdir(parents=True)
            config = home / ".nebius/config.yaml"
            config.write_text("default: keep\nprofiles:\n    keep:\n    omarchy-nebius-mcp:\n")
            credentials = home / ".nebius/credentials.yaml"
            credentials.write_text(
                "tokens:\n"
                "    federation/auth.nebius.com//keep:\n        token: keep-token\n        expires_at: 1\n"
                "    federation/auth.nebius.com//omarchy-nebius-mcp:\n"
                "        token: plugin-token\n        expires_at: 2\n"
            )
            ssh_key = home / ".ssh/nebius-ed25519"
            ssh_key.parent.mkdir()
            ssh_key.write_text("private")
            ssh_key.with_suffix(".pub").write_text("public")
            bindings = home / ".config/hypr/bindings.lua"
            bindings.parent.mkdir(parents=True)
            bindings.write_text(
                "before\n-- >>> independent Nebius plugin shortcuts >>>\nplugin lines\n"
                "-- <<< independent Nebius plugin shortcuts <<<\nafter\n"
            )
            cloud_marker = home / "cloud-resource"
            cloud_marker.write_text("must remain")
            calls = home / "calls"
            calls.mkdir()

            self.write_command(fake_bin / "codex", f'''#!/bin/sh
if [ "$1 $2" = "mcp get" ]; then
  printf '%s\\n' '{{"command":"python3","args":["{plugin}/libexec/nebius_agent_mcp.py"]}}'
elif [ "$1 $2" = "mcp remove" ]; then
  : >"{calls}/mcp-removed"
fi
''')
            (home / ".claude.json").write_text(json.dumps({
                "mcpServers": {
                    "nebius": {
                        "type": "stdio",
                        "command": "python3",
                        "args": [str(plugin / "libexec/nebius_agent_mcp.py")],
                    }
                }
            }))
            self.write_command(fake_bin / "claude", f'''#!/bin/sh
if [ "$1 $2 $3 $4" = "mcp remove nebius --scope" ] && [ "$5" = "user" ]; then
  : >"{calls}/claude-mcp-removed"
fi
''')
            self.write_command(nebius_dir / "nebius", f'''#!/bin/sh
case "$1 $2" in
  "profile list") printf '%s\\n' 'keep [default]' 'omarchy-nebius-mcp' ;;
  "profile active") printf '%s\\n' keep ;;
  "profile delete") : >"{calls}/profile-deleted" ;;
esac
''')
            self.write_command(fake_bin / "pacman", "#!/bin/sh\nexit 1\n")
            self.write_command(fake_bin / "flock", "#!/bin/sh\nexit 0\n")
            self.write_command(fake_bin / "hyprctl", "#!/bin/sh\nexit 0\n")
            self.write_command(fake_bin / "omarchy", f'''#!/bin/sh
rm -rf -- "{plugin}"
printf '%s\\n' 'Removed nebius.'
''')

            result = self.run_uninstall(home, "--yes", path=str(fake_bin) + os.pathsep + os.environ["PATH"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(plugin.exists())
            self.assertFalse(state_dir.exists())
            self.assertFalse(private_cache.exists())
            self.assertEqual(other_cache.read_text(), "keep")
            self.assertFalse(ssh_key.exists())
            self.assertFalse(ssh_key.with_suffix(".pub").exists())
            self.assertEqual(bindings.read_text(), "before\nafter\n")
            self.assertTrue((calls / "mcp-removed").exists())
            self.assertTrue((calls / "claude-mcp-removed").exists())
            self.assertTrue((calls / "profile-deleted").exists())
            self.assertIn("//keep:", credentials.read_text())
            self.assertNotIn("//omarchy-nebius-mcp:", credentials.read_text())
            self.assertIn("keep-token", credentials.read_text())
            self.assertNotIn("plugin-token", credentials.read_text())
            self.assertEqual(cloud_marker.read_text(), "must remain")
            self.assertIn("Nebius plugin setup removed", result.stdout)
            self.assertIn("Cloud resources were not changed", result.stdout)

    def test_explicit_keep_preserves_cli_and_ssh_key(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin = home / "fake-bin"
            fake_bin.mkdir()
            plugin = home / ".config/omarchy/plugins/nebius"
            plugin.mkdir(parents=True)
            state_dir = home / ".state/nebius"
            state_dir.mkdir(parents=True)
            (state_dir / "setup.log").write_text("✓ Installed and verified Nebius CLI 0.12.269\n")
            cli = home / ".nebius/bin/nebius"
            cli.parent.mkdir(parents=True)
            self.write_command(cli, "#!/bin/sh\nexit 0\n")
            ssh_key = home / ".ssh/nebius-ed25519"
            ssh_key.parent.mkdir()
            ssh_key.write_text("private")
            ssh_key.with_suffix(".pub").write_text("public")

            self.write_command(fake_bin / "codex", "#!/bin/sh\nexit 1\n")
            self.write_command(fake_bin / "pacman", "#!/bin/sh\nexit 1\n")
            self.write_command(fake_bin / "flock", "#!/bin/sh\nexit 0\n")
            self.write_command(fake_bin / "omarchy", f'''#!/bin/sh
rm -rf -- "{plugin}"
printf '%s\\n' 'Removed nebius.'
''')

            result = self.run_uninstall(
                home, "--yes", "--keep-cli", "--keep-ssh-key",
                path=str(fake_bin) + os.pathsep + os.environ["PATH"],
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(cli.exists())
            self.assertTrue(ssh_key.exists())
            self.assertTrue(ssh_key.with_suffix(".pub").exists())
            self.assertIn("Kept the Nebius CLI for reuse", result.stdout)
            self.assertIn("Kept the dedicated SSH key", result.stdout)

    def test_only_plugin_credential_file_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin = home / "fake-bin"
            fake_bin.mkdir()
            plugin = home / ".config/omarchy/plugins/nebius"
            plugin.mkdir(parents=True)
            state_dir = home / ".state/nebius"
            state_dir.mkdir(parents=True)
            credentials = home / ".nebius/credentials.yaml"
            credentials.parent.mkdir(parents=True)
            credentials.write_text(
                "tokens:\n    federation/auth.nebius.com//omarchy-nebius-mcp:\n"
                "        token: plugin-token\n        expires_at: 2\n"
            )
            self.write_command(fake_bin / "codex", "#!/bin/sh\nexit 1\n")
            self.write_command(fake_bin / "pacman", "#!/bin/sh\nexit 1\n")
            self.write_command(fake_bin / "flock", "#!/bin/sh\nexit 0\n")
            self.write_command(fake_bin / "omarchy", f'''#!/bin/sh
rm -rf -- "{plugin}"
printf '%s\\n' 'Removed nebius.'
''')

            result = self.run_uninstall(home, "--yes", path=str(fake_bin) + os.pathsep + os.environ["PATH"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(credentials.exists())

    @staticmethod
    def write_command(path: Path, contents: str):
        path.write_text(contents)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
