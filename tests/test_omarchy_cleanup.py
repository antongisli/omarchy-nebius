"""Optional Linux integration: real Omarchy removal + real Nebius cleanup, fake home/IPC."""

import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
OMARCHY = os.environ.get("NEBIUS_TEST_OMARCHY_SOURCE")


@unittest.skipUnless(OMARCHY, "Opt-in: requires the Omarchy cleanup-hook source on Linux")
class OmarchyCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nebius-hook-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.plugin = self.home / ".config/omarchy/plugins/nebius"
        shutil.copytree(ROOT, self.plugin, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        (self.plugin / ".git").mkdir()
        self.state = self.home / ".state/nebius"
        self.state.mkdir(parents=True)
        (self.state / "install-receipt.json").write_text(json.dumps({"owned": {"uv": False, "cli": False}}))
        (self.state / "ports.json").write_text("[]")
        self.cache = self.home / ".cache/nebius/uv"
        self.cache.mkdir(parents=True)
        (self.cache / "runtime").write_text("private cache")
        self.unit = self.home / ".config/systemd/user/nebius-ports.service"
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("fixture service")
        self.key = self.home / ".ssh/nebius-ed25519"
        self.key.parent.mkdir()
        self.key.write_text("fixture key")
        for name in ("cloud-resources", "unrelated-config", "mcp-registration", "service-running"):
            (self.home / name).write_text("keep")
        self.stubs = self.home / "bin"
        self.stubs.mkdir()
        self.command("omarchy-shell", '''#!/bin/bash
case "$2" in
listPlugins)
  if [[ ! -d $HOME/.config/omarchy/plugins/nebius ]]; then echo '[]'
  elif [[ -f $HOME/widget-disabled ]]; then echo '[{"id":"nebius","enabled":false,"active":false}]'
  else echo '[{"id":"nebius","enabled":true,"active":true}]'; fi ;;
setPluginEnabled) touch "$HOME/widget-disabled"; echo ok ;;
rescanPlugins) echo ok ;;
esac
''')
        self.command("systemctl", '''#!/bin/bash
case "$2" in
disable) rm -f "$HOME/service-running" ;;
is-active) [[ -f $HOME/service-running ]] ;;
*) exit 0 ;;
esac
''')
        self.command("codex", '''#!/bin/bash
case "$1 $2" in
"mcp get")
  [[ -f $HOME/mcp-registration ]] || exit 1
  printf '{"transport":{"command":"python3","args":["%s/.config/omarchy/plugins/nebius/libexec/nebius_agent_mcp.py"]}}\\n' "$HOME" ;;
"mcp remove") rm "$HOME/mcp-registration" ;;
*) exit 1 ;;
esac
''')
        self.command("gum", '#!/bin/bash\nprintf "Cancel\\n"\n')
        self.env = {**os.environ, "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
                    "XDG_STATE_HOME": str(self.home / ".state"), "XDG_CACHE_HOME": str(self.home / ".cache"),
                    "XDG_DATA_HOME": str(self.home / ".data"), "XDG_RUNTIME_DIR": str(self.home / "runtime"),
                    "OMARCHY_PATH": str(OMARCHY), "PATH": f"{self.stubs}:{OMARCHY}/bin:/usr/bin:/bin"}
        self.env.pop("NEBIUS_CLI_BIN", None)
        self.env.pop("OMARCHY_PLUGIN_REMOVAL_ID", None)
        self.env.pop("OMARCHY_PLUGIN_DIR", None)

    def command(self, name, contents):
        path = self.stubs / name
        path.write_text(contents)
        path.chmod(0o700)

    def assert_clean(self):
        for path in (self.plugin, self.state, self.cache, self.unit,
                     self.home / "mcp-registration", self.home / "service-running"):
            self.assertFalse(path.exists(), f"Uninstall left {path}")
        for path in (self.key, self.home / "cloud-resources", self.home / "unrelated-config"):
            self.assertEqual(path.read_text(), "fixture key" if path == self.key else "keep")
        self.assertTrue((self.home / "widget-disabled").exists())

    def test_generic_omarchy_remove_runs_nebius_cleanup(self):
        result = subprocess.run(["omarchy", "plugin", "remove", "nebius", "--yes"],
                                env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Cleanup completed for nebius", result.stdout)
        self.assert_clean()

    def test_panel_command_cleans_once_without_hook_recursion(self):
        result = subprocess.run([str(self.plugin / "bin/nebius-uninstall"), "--yes"],
                                env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Local Nebius plugin setup removed", result.stdout)
        self.assertNotIn("Running cleanup for nebius", result.stdout)
        self.assert_clean()

    def test_agent_returns_verified_result_after_removing_its_registration(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "uninstall_plugin", "arguments": {
                "confirmed": True, "keep_cli": True, "keep_ssh_key": True, "keep_uv": True}}}
        result = subprocess.run(["python3", str(self.plugin / "libexec/nebius_agent_mcp.py")],
                                input=json.dumps(request) + "\n", env=self.env, text=True,
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)["result"]
        self.assertFalse(payload["isError"], payload)
        self.assertEqual(payload["structuredContent"]["status"], "removed")
        self.assert_clean()

    def test_canceling_nebius_cleanup_does_not_remove_plugin(self):
        command = shlex.join([str(self.plugin / "bin/nebius-cleanup")])
        env = {**self.env, "OMARCHY_PLUGIN_REMOVAL_ID": "nebius", "OMARCHY_PLUGIN_DIR": str(self.plugin)}
        result = subprocess.run(["script", "-qec", command, "/dev/null"], env=env,
                                text=True, capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(self.plugin.exists())
        self.assertTrue(self.state.exists())
        self.assertTrue(self.unit.exists())
