"""Agent configuration is changed only by an explicit per-agent choice."""

import curses
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libexec"))
import nebius_ui as ui
from test_agents import load_module
from test_ui import app


class SetupDoesNotEditAgentsTests(unittest.TestCase):
    def test_setup_never_registers_or_removes_agent_tools(self):
        source = (ROOT / "bin/nebius-setup").read_text()
        for forbidden in ('"$CODEX_MCP" ensure', '"$CLAUDE_MCP" ensure', "mcp add", "mcp remove",
                          '"$agent_script" ensure', '"$agent_script" remove'):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("mark_owned codex_mcp", source)
        self.assertNotIn("mark_owned claude_mcp", source)


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module("nebius_claude_mcp_opt_in", ROOT / "libexec/nebius_claude_mcp.py")
        self.temp = tempfile.TemporaryDirectory()
        home = Path(self.temp.name)
        self.module.HOME = home
        self.module.CONFIG = home / ".claude.json"
        self.module.AGENT_MCP = home / "nebius_agent_mcp.py"

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, servers):
        self.module.CONFIG.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")

    def test_remove_refuses_an_unrelated_server(self):
        self.write_config({"nebius": {"type": "stdio", "command": "other", "args": []}})
        with mock.patch.object(self.module.subprocess, "run") as run, \
             self.assertRaisesRegex(self.module.RegistrationError, "did not create"):
            self.module.remove()
        run.assert_not_called()

    def test_remove_deletes_only_the_exact_registration(self):
        self.write_config({"nebius": {"type": "stdio", "command": "python3", "args": [str(self.module.AGENT_MCP)]}})

        def fake_run(arguments, **_kwargs):
            self.write_config({})
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with mock.patch.object(self.module.shutil, "which", return_value="/usr/bin/claude"), \
             mock.patch.object(self.module.subprocess, "run", side_effect=fake_run) as run:
            result = self.module.remove()
        self.assertFalse(result["ready"])
        self.assertEqual(run.call_args.args[0], ["/usr/bin/claude", "mcp", "remove", "--scope", "user", "nebius"])

    def test_codex_status_reports_missing_codex_without_running_it(self):
        codex = load_module("nebius_codex_mcp_opt_in", ROOT / "libexec/nebius_codex_mcp.py")
        codex.HOME = Path(self.temp.name)
        with mock.patch.object(codex.shutil, "which", return_value=None), \
             mock.patch.object(codex.subprocess, "run") as run:
            self.assertEqual(codex.status()["installed"], False)
        run.assert_not_called()


class SettingsToggleTests(unittest.TestCase):
    def fake_agent(self, ready=False):
        module = mock.Mock()
        module.RegistrationError = RuntimeError
        module.AGENT_MCP = Path("/plugin/libexec/nebius_agent_mcp.py")
        module.status.return_value = {"installed": True, "ready": ready}
        return module

    def toggle(self, module, confirmed):
        application, _screen = app([])
        tools = {"codex": ("Codex", module, "~/.codex/config.toml")}
        with mock.patch.object(ui.App, "AGENT_TOOLS", tools), \
             mock.patch.object(application, "confirm_launch", return_value=confirmed) as confirm:
            application.toggle_agent_tools("codex")
        return confirm

    def test_cancelled_confirmation_leaves_agent_configuration_unchanged(self):
        module = self.fake_agent()
        confirm = self.toggle(module, False)
        confirm.assert_called_once()
        module.ensure.assert_not_called()
        module.remove.assert_not_called()

    def test_confirmed_add_and_remove_call_only_the_chosen_agent(self):
        module = self.fake_agent()
        self.toggle(module, True)
        module.ensure.assert_called_once_with()
        module.remove.assert_not_called()
        added = self.fake_agent(ready=True)
        self.toggle(added, True)
        added.remove.assert_called_once_with()
        added.ensure.assert_not_called()


class WidgetPlainTextTests(unittest.TestCase):
    def widget(self):
        manifest = json.loads((ROOT / "manifest.json").read_text())
        return (ROOT / manifest["entryPoints"]["barWidget"]).read_text()

    def test_every_text_element_is_plain_text(self):
        source = self.widget()
        blocks = re.findall(r"\n\s*Text \{\n(.*?)\n\s*\}", source, re.S)
        self.assertGreater(len(blocks), 0)
        for block in blocks:
            self.assertIn("textFormat: Text.PlainText", block)

    def test_tooltip_strips_markup_delimiters(self):
        source = self.widget()
        self.assertIn('replace(/[<>]/g, "")', source)
        self.assertIn("barTooltip: plainTooltip(", source)


if __name__ == "__main__":
    unittest.main()
