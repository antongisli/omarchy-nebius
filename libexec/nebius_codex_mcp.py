#!/usr/bin/env python3
"""Register and verify the plugin-owned MCP server in Codex."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
from typing import Any


HOME = Path.home()
SERVER_NAME = "nebius"
AGENT_MCP = HOME / ".config/omarchy/plugins/nebius/libexec/nebius_agent_mcp.py"
CONFIG = HOME / ".codex/config.toml"
SETTINGS: dict[str, Any] = {
    "startup_timeout_sec": 30,
    "tool_timeout_sec": 1200,
    "required": False,
    "default_tools_approval_mode": "writes",
}


class RegistrationError(RuntimeError):
    pass


def _codex() -> str:
    candidate = shutil.which("codex")
    if candidate:
        return candidate
    fallback = HOME / ".local/share/mise/shims/codex"
    if fallback.is_file():
        return str(fallback)
    raise RegistrationError("Codex is not installed")


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def _current() -> dict[str, Any] | None:
    result = _run([_codex(), "mcp", "get", SERVER_NAME, "--json"])
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RegistrationError("Codex returned invalid MCP configuration") from error
    return value if isinstance(value, dict) else None


def _matches(value: dict[str, Any] | None) -> bool:
    if not value:
        return False
    transport = value.get("transport") if isinstance(value.get("transport"), dict) else value
    return transport.get("command") == "python3" and transport.get("args") == [str(AGENT_MCP)]


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)


def _set_server_settings() -> None:
    text = CONFIG.read_text(encoding="utf-8")
    heading = f"[mcp_servers.{SERVER_NAME}]"
    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == heading)
    except StopIteration as error:
        raise RegistrationError("Codex did not create the expected MCP configuration table") from error
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^\s*\[", lines[index]):
            end = index
            break
    section = lines[start:end]
    for key, value in SETTINGS.items():
        replacement = f"{key} = {_toml_literal(value)}"
        match = next((index for index, line in enumerate(section) if re.match(rf"^\s*{re.escape(key)}\s*=", line)), None)
        if match is None:
            section.append(replacement)
        else:
            section[match] = replacement
    lines[start:end] = section
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    with CONFIG.open("rb") as handle:
        tomllib.load(handle)


def ensure() -> dict[str, Any]:
    if not AGENT_MCP.is_file():
        raise RegistrationError(f"Plugin MCP entry point is missing: {AGENT_MCP}")
    current = _current()
    if current and not _matches(current):
        raise RegistrationError(
            f"Codex already has an MCP server named {SERVER_NAME} with a different command; remove or rename it first"
        )
    if not current:
        result = _run([_codex(), "mcp", "add", SERVER_NAME, "--", "python3", str(AGENT_MCP)])
        if result.returncode != 0:
            raise RegistrationError(result.stderr.strip() or "Could not register the Codex MCP server")
    _set_server_settings()
    return status()


def status() -> dict[str, Any]:
    current = _current()
    ready = _matches(current)
    settings_ready = False
    if ready and CONFIG.is_file():
        try:
            with CONFIG.open("rb") as handle:
                parsed = tomllib.load(handle)
            configured = parsed.get("mcp_servers", {}).get(SERVER_NAME, {})
            settings_ready = all(configured.get(key) == value for key, value in SETTINGS.items())
        except (OSError, tomllib.TOMLDecodeError):
            settings_ready = False
    return {
        "ready": ready and settings_ready,
        "detail": "Constrained tools registered" if ready and settings_ready else "Needs registration",
        "server": SERVER_NAME,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("ensure", "status"))
    args = parser.parse_args()
    try:
        print(json.dumps(ensure() if args.command == "ensure" else status(), sort_keys=True))
        return 0
    except RegistrationError as error:
        print(f"Nebius Codex MCP: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
