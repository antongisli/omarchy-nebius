"""Uninstaller boundary: local setup is removed; cloud state is untouched."""

from __future__ import annotations

import os
from pathlib import Path
import json
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import nebius_uninstall as removal


ROOT = Path(__file__).resolve().parents[1]
UNINSTALL = ROOT / "bin/nebius-uninstall"


class UninstallTests(unittest.TestCase):
    def run_uninstall(self, home: Path, *arguments: str, path: str | None = None):
        environment = {**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / ".state")}
        environment["XDG_CACHE_HOME"] = str(home / ".cache")
        if path:
            environment["PATH"] = path
            fake_bin = Path(path.split(os.pathsep)[0])
            if not (fake_bin / "python3").exists():
                self.write_command(fake_bin / "python3", f'''#!/bin/sh
exec "{sys.executable}" "$@"
''')
            if not (fake_bin / "omarchy-shell").exists():
                self.write_command(fake_bin / "omarchy-shell", '''#!/bin/sh
if [ -d "$HOME/.config/omarchy/plugins/nebius" ]; then
  printf '%s\\n' '[{"id":"nebius","enabled":false,"active":false}]'
else
  printf '[]\\n'
fi
''')
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

    def test_explicit_removal_cleans_local_setup_but_not_cloud_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin = home / "fake-bin"
            fake_bin.mkdir()
            plugin = home / ".config/omarchy/plugins/nebius"
            (plugin / "libexec").mkdir(parents=True)
            (plugin / "libexec/nebius_agent_mcp.py").write_text("# test")
            launcher = plugin / "bin/nebius-cli"
            launcher.parent.mkdir()
            self.write_command(launcher, "#!/bin/sh\nexit 0\n")
            state_dir = home / ".state/nebius"
            state_dir.mkdir(parents=True)
            (state_dir / "setup.log").write_text("setup")
            terminal_command = home / ".local/bin/nebius"
            terminal_command.parent.mkdir(parents=True)
            terminal_command.symlink_to(launcher)
            (state_dir / "install-receipt.json").write_text(json.dumps({
                "owned": {"cli_link": True},
                "cli_link_path": str(terminal_command),
                "cli_link_target": str(launcher),
            }))
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
                "-- before\n-- >>> independent Nebius plugin shortcuts >>>\n-- plugin lines\n"
                "-- <<< independent Nebius plugin shortcuts <<<\n-- after\n"
            )
            cloud_marker = home / "cloud-resource"
            cloud_marker.write_text("must remain")
            calls = home / "calls"
            calls.mkdir()

            self.write_command(fake_bin / "codex", f'''#!/bin/sh
if [ "$1 $2" = "mcp get" ]; then
  [ ! -e "{calls}/mcp-removed" ] || exit 1
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
  printf '%s\\n' '{{"mcpServers":{{}}}}' >"$HOME/.claude.json"
fi
''')
            self.write_command(nebius_dir / "nebius", f'''#!/bin/sh
case "$1 $2" in
  "profile list") printf '%s\\n' 'keep [default]' 'omarchy-nebius-mcp' ;;
  "profile active") printf '%s\\n' keep ;;
  "profile delete") : >"{calls}/profile-deleted"; printf 'default: keep\\nprofiles:\\n    keep:\\n' >"$HOME/.nebius/config.yaml" ;;
esac
''')
            self.write_command(fake_bin / "pacman", "#!/bin/sh\nexit 1\n")
            self.write_command(fake_bin / "flock", "#!/bin/sh\nexit 0\n")
            self.write_command(fake_bin / "hyprctl", "#!/bin/sh\necho '[]'\n")
            self.write_command(fake_bin / "systemctl", "#!/bin/sh\nexit 0\n")
            self.write_command(fake_bin / "luac", "#!/bin/sh\nexit 0\n")
            self.write_command(fake_bin / "omarchy", f'''#!/bin/sh
[ "$3" != "--help" ] || exit 0
rm -rf -- "{plugin}"
printf '%s\\n' 'Removed nebius.'
''')

            result = self.run_uninstall(home, "--yes", "--remove-ssh-key", path=str(fake_bin) + os.pathsep + os.environ["PATH"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(plugin.exists())
            self.assertFalse(state_dir.exists())
            self.assertFalse(private_cache.exists())
            self.assertFalse(terminal_command.exists())
            self.assertFalse(terminal_command.is_symlink())
            self.assertEqual(other_cache.read_text(), "keep")
            self.assertFalse(ssh_key.exists())
            self.assertFalse(ssh_key.with_suffix(".pub").exists())
            self.assertEqual(bindings.read_text(), "-- before\n-- after\n")
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

    def test_changed_terminal_command_is_preserved_on_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin, plugin, state, key = self.fixture(home)
            launcher = plugin / "bin/nebius-cli"
            launcher.parent.mkdir()
            self.write_command(launcher, "#!/bin/sh\nexit 0\n")
            replacement = home / "user-nebius"
            self.write_command(replacement, "#!/bin/sh\nexit 0\n")
            command = home / ".local/bin/nebius"
            command.parent.mkdir(parents=True)
            command.symlink_to(replacement)
            (state / "install-receipt.json").write_text(json.dumps({
                "owned": {"cli_link": True},
                "cli_link_path": str(command),
                "cli_link_target": str(launcher),
            }))

            result = self.run_uninstall(home, "--yes", path=str(fake_bin) + os.pathsep + os.environ["PATH"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(command.is_symlink())
            self.assertEqual(os.readlink(command), str(replacement))
            self.assertIn("no longer matches the plugin-created link", result.stdout)

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
[ "$3" != "--help" ] || exit 0
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
[ "$3" != "--help" ] || exit 0
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

    def fixture(self, home):
        fake_bin = home / "fake-bin"
        fake_bin.mkdir()
        plugin = home / ".config/omarchy/plugins/nebius"
        plugin.mkdir(parents=True)
        state = home / ".state/nebius"
        state.mkdir(parents=True)
        (state / "keep").write_text("recovery record")
        key = home / ".ssh/nebius-ed25519"
        key.parent.mkdir()
        key.write_text("keep access")
        self.write_command(fake_bin / "codex", "#!/bin/sh\nexit 1\n")
        self.write_command(fake_bin / "pacman", "#!/bin/sh\nexit 1\n")
        self.write_command(fake_bin / "flock", "#!/bin/sh\nexit 0\n")
        self.write_command(fake_bin / "omarchy", '''#!/bin/sh
case "$2 $3" in
  "remove --help") echo '--skip-cleanup'; exit 0 ;;
  "remove nebius") rm -rf -- "$HOME/.config/omarchy/plugins/nebius" ;;
esac
''')
        return fake_bin, plugin, state, key

    def test_unavailable_shell_preserves_all_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin, plugin, state, key = self.fixture(home)
            self.write_command(fake_bin / "omarchy-shell", "#!/bin/sh\necho 'OMARCHY_PATH is not set' >&2\nexit 1\n")
            result = self.run_uninstall(home, "--yes", path=str(fake_bin) + os.pathsep + os.environ["PATH"])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Nothing was removed", result.stderr)
            self.assertTrue(plugin.exists())
            self.assertTrue((state / "keep").exists())
            self.assertTrue(key.exists())

    def test_yes_defaults_to_keeping_access_key(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin, plugin, state, key = self.fixture(home)
            result = self.run_uninstall(home, "--yes", path=str(fake_bin) + os.pathsep + os.environ["PATH"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(key.exists())
            self.assertFalse(plugin.exists())
            self.assertFalse(state.exists())

    def test_false_success_from_omarchy_preserves_cleanup_records(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin, plugin, state, key = self.fixture(home)
            self.write_command(fake_bin / "omarchy", "#!/bin/sh\necho 'Removed nebius.'\n")
            result = self.run_uninstall(home, "--yes", path=str(fake_bin) + os.pathsep + os.environ["PATH"])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("left the plugin directory", result.stderr)
            self.assertTrue((state / "keep").exists())
            self.assertTrue(key.exists())

    def test_widget_must_be_unloaded_before_local_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin, plugin, state, key = self.fixture(home)
            self.write_command(fake_bin / "omarchy-shell", '''#!/bin/sh
echo '[{"id":"nebius","enabled":true,"active":true}]'
''')
            result = self.run_uninstall(home, "--yes", path=str(fake_bin) + os.pathsep + os.environ["PATH"])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("widget is still enabled", result.stderr)
            self.assertTrue((state / "keep").exists())

    def test_check_is_read_only_and_does_not_claim_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin, plugin, state, key = self.fixture(home)
            result = self.run_uninstall(home, "--check", path=str(fake_bin) + os.pathsep + os.environ["PATH"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("preflight passed", result.stdout)
            self.assertTrue(plugin.exists())
            self.assertEqual(sorted(p.name for p in state.iterdir()), ["keep"])

    def test_checkout_can_finish_cleanup_when_plugin_bundle_is_already_gone(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin, plugin, state, key = self.fixture(home)
            plugin.rmdir()
            self.write_command(fake_bin / "omarchy", "#!/bin/sh\necho 'Unexpected native remove' >&2\nexit 1\n")
            self.write_command(fake_bin / "omarchy-shell", '''#!/bin/sh
case "$2" in
  rescanPlugins) touch "$HOME/rescanned"; echo ok ;;
  listPlugins) echo '[]' ;;
esac
''')
            for _ in range(2):
                result = self.run_uninstall(home, "--yes", path=str(fake_bin) + os.pathsep + os.environ["PATH"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Local Nebius plugin setup removed.", result.stdout)
                self.assertFalse(plugin.exists())
                self.assertFalse(state.exists())
                self.assertTrue(key.exists())
            self.assertTrue((home / "rescanned").exists())

    def test_missing_bundle_with_stale_registry_retains_cleanup_records(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin, plugin, state, key = self.fixture(home)
            plugin.rmdir()
            self.write_command(fake_bin / "omarchy-shell", '''#!/bin/sh
case "$2" in
  rescanPlugins) echo ok ;;
  listPlugins) echo '[{"id":"nebius","enabled":false,"active":false}]' ;;
esac
''')
            result = self.run_uninstall(home, "--yes", path=str(fake_bin) + os.pathsep + os.environ["PATH"])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Omarchy still lists the plugin", result.stderr)
            self.assertTrue((state / "keep").exists())
            self.assertTrue(key.exists())

    def test_cleanup_hook_leaves_plugin_removal_to_host(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin, plugin, state, key = self.fixture(home)
            with mock.patch.dict(os.environ, {"OMARCHY_PLUGIN_REMOVAL_ID": "nebius", "OMARCHY_PLUGIN_DIR": str(plugin)}):
                result = self.run_uninstall(home, "--from-omarchy", "--yes", path=str(fake_bin) + os.pathsep + os.environ["PATH"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(plugin.exists())
            self.assertFalse(state.exists())
            self.assertIn("Omarchy will now remove", result.stdout)
            self.assertNotIn("Local Nebius plugin setup removed.", result.stdout)


class AgentUninstallTests(unittest.TestCase):
    def test_missing_agent_cli_cannot_silently_leave_owned_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".codex/config.toml"
            config.parent.mkdir()
            expected = str(home / ".config/omarchy/plugins/nebius/libexec/nebius_agent_mcp.py")
            config.write_text('[mcp_servers.nebius]\ncommand = "python3"\nargs = ' + json.dumps([expected]) + '\n')
            with mock.patch.object(removal.Path, "home", return_value=home), mock.patch.object(removal.shutil, "which", return_value=None):
                with self.assertRaisesRegex(removal.UninstallError, "CLI is unavailable"):
                    removal.check_agent_configs()
                with self.assertRaisesRegex(removal.UninstallError, "still has"):
                    removal.check_agent_configs(verify=True)

    def test_unrelated_agent_registration_is_not_claimed_or_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".claude.json"
            original = json.dumps({"mcpServers": {"nebius": {"command": "other", "args": []}}})
            config.write_text(original)
            with mock.patch.object(removal.Path, "home", return_value=home):
                removal.check_agent_configs(verify=True)
            self.assertEqual(config.read_text(), original)

    def test_plan_checks_without_removing(self):
        with mock.patch.object(removal, "_run", return_value=subprocess.CompletedProcess([], 0, "Ready", "")) as run:
            self.assertTrue(removal.plan()["ready"])
        self.assertEqual(run.call_args.args, (["--check"], 30))

    def test_confirmation_and_explicit_choices_are_required(self):
        with mock.patch.object(removal, "_run") as run:
            for value in (False, "true", 1, None):
                with self.assertRaises(removal.UninstallError):
                    removal.uninstall(confirmed=value, keep_cli=True, keep_ssh_key=True, keep_uv=True)
            with self.assertRaises(removal.UninstallError):
                removal.uninstall(confirmed=True, keep_cli=True, keep_ssh_key="false", keep_uv=True)
            run.assert_not_called()

    def test_agent_and_panel_share_the_same_uninstaller(self):
        with mock.patch.object(removal, "_run", return_value=subprocess.CompletedProcess([], 0, "Local Nebius plugin setup removed.", "")) as run:
            result = removal.uninstall(confirmed=True, keep_cli=True, keep_ssh_key=True, keep_uv=True)
        self.assertEqual(result["status"], "removed")
        self.assertEqual(run.call_args.args[0], ["--yes", "--keep-cli", "--keep-ssh-key", "--keep-uv"])
        self.assertFalse(result["cloud_resources_changed"])

    def test_failed_or_unverified_removal_never_reports_success(self):
        for code, output in ((1, "Failed"), (0, "Uninstall cancelled")):
            with mock.patch.object(removal, "_run", return_value=subprocess.CompletedProcess([], code, output, "")):
                with self.assertRaisesRegex(removal.UninstallError, "Do not report success"):
                    removal.uninstall(confirmed=True, keep_cli=True, keep_ssh_key=True, keep_uv=True)


if __name__ == "__main__":
    unittest.main()
