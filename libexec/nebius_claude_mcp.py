#!/usr/bin/env python3
"""Register and verify the plugin-owned MCP server in Claude Code."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


HOME = Path.home()
SERVER_NAME = "nebius"
AGENT_MCP = HOME / ".config/omarchy/plugins/nebius/libexec/nebius_agent_mcp.py"
CONFIG = HOME / ".claude.json"


class RegistrationError(RuntimeError):
    pass


def _claude() -> str:
    candidate = shutil.which("claude")
    if candidate:
        return candidate
    fallback = HOME / ".local/bin/claude"
    if fallback.is_file():
        return str(fallback)
    raise RegistrationError("Claude Code is not installed")


def _config() -> dict[str, Any]:
    if not CONFIG.is_file():
        return {}
    try:
        value = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RegistrationError("Claude Code configuration is not valid JSON") from error
    return value if isinstance(value, dict) else {}


def _current() -> dict[str, Any] | None:
    value = _config().get("mcpServers", {}).get(SERVER_NAME)
    return value if isinstance(value, dict) else None


def _matches(value: dict[str, Any] | None) -> bool:
    return bool(value and value.get("type", "stdio") == "stdio"
                and value.get("command") == "python3" and value.get("args") == [str(AGENT_MCP)])


def status() -> dict[str, Any]:
    installed = bool(shutil.which("claude") or (HOME / ".local/bin/claude").is_file())
    ready = installed and _matches(_current())
    return {
        "installed": installed,
        "ready": ready,
        "detail": "Constrained tools registered" if ready else "Needs registration" if installed else "Not installed",
        "server": SERVER_NAME,
    }


def ensure() -> dict[str, Any]:
    if not AGENT_MCP.is_file():
        raise RegistrationError(f"Plugin MCP entry point is missing: {AGENT_MCP}")
    current = _current()
    if current and not _matches(current):
        raise RegistrationError(
            f"Claude Code already has an MCP server named {SERVER_NAME} with a different command; remove or rename it first"
        )
    if not current:
        result = subprocess.run(
            [_claude(), "mcp", "add", "--scope", "user", SERVER_NAME, "--", "python3", str(AGENT_MCP)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode != 0:
            raise RegistrationError(result.stderr.strip() or "Could not register the Claude Code MCP server")
    value = status()
    if not value["ready"]:
        raise RegistrationError("Claude Code did not retain the Nebius MCP registration")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("ensure", "status"))
    args = parser.parse_args()
    try:
        print(json.dumps(ensure() if args.command == "ensure" else status(), sort_keys=True))
        return 0
    except RegistrationError as error:
        print(f"Nebius Claude Code MCP: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
