"""Supported-agent MCP registration contracts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClaudeRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module("nebius_claude_mcp_test", ROOT / "libexec/nebius_claude_mcp.py")
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.module.HOME = self.home
        self.module.CONFIG = self.home / ".claude.json"
        self.module.AGENT_MCP = self.home / ".config/omarchy/plugins/nebius/libexec/nebius_agent_mcp.py"
        self.module.AGENT_MCP.parent.mkdir(parents=True)
        self.module.AGENT_MCP.write_text("# test\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, server: dict):
        self.module.CONFIG.write_text(json.dumps({"mcpServers": {"nebius": server}}), encoding="utf-8")

    def test_status_accepts_only_the_exact_plugin_registration(self):
        self.write_config({
            "type": "stdio",
            "command": "python3",
            "args": [str(self.module.AGENT_MCP)],
            "env": {},
        })
        with mock.patch.object(self.module.shutil, "which", return_value="/usr/bin/claude"):
            self.assertTrue(self.module.status()["ready"])

    def test_ensure_refuses_to_replace_an_unrelated_server(self):
        self.write_config({"type": "stdio", "command": "other", "args": []})
        with self.assertRaisesRegex(self.module.RegistrationError, "different command"):
            self.module.ensure()

    def test_ensure_uses_claude_user_scope(self):
        def fake_run(arguments, **_kwargs):
            self.write_config({
                "type": "stdio",
                "command": "python3",
                "args": [str(self.module.AGENT_MCP)],
                "env": {},
            })
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with mock.patch.object(self.module.shutil, "which", return_value="/usr/bin/claude"), \
             mock.patch.object(self.module.subprocess, "run", side_effect=fake_run) as run:
            result = self.module.ensure()

        self.assertTrue(result["ready"])
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/claude", "mcp", "add", "--scope", "user", "nebius", "--", "python3", str(self.module.AGENT_MCP)],
        )


if __name__ == "__main__":
    unittest.main()
