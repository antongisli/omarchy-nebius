#!/usr/bin/env python3
"""Narrow, plugin-owned JSON-RPC bridge for the pinned Nebius MCP server."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any

from nebius_runtime import CLIENT_NAME, VERSION, cli_environment, cli_path


MCP_COMMIT = "6388bf779acdd331d9b2016230b37f8bf7177e12"
MCP_SPEC = f"nebius-mcp-server@git+https://github.com/nebius/mcp-server@{MCP_COMMIT}"
PROFILE_NAME = "omarchy-nebius-mcp"
EXPECTED_TOOLS = {
    "nebius_profiles",
    "nebius_available_services",
    "nebius_cli_help",
    "nebius_cli_execute",
}
FIXED_ACTIONS: dict[str, tuple[str, dict[str, str]]] = {
    "profiles": ("nebius_profiles", {}),
    "services": ("nebius_available_services", {}),
    "projects": (
        "nebius_cli_execute",
        {"command": ""},
    ),
}


class BridgeError(RuntimeError):
    pass


class McpClient:
    def __init__(self, *, refresh: bool = False) -> None:
        home = Path.home()
        uvx = os.environ.get("NEBIUS_UVX", "uvx")
        command = [uvx]
        if refresh:
            command.extend(["--refresh-package", "nebius-mcp-server"])
        command.append(MCP_SPEC)

        environment = cli_environment()
        environment.update(
            {
                "SAFE_MODE": "false",
                "NEBIUS_CLI_BIN": str(cli_path()),
                "NEBIUS_PROFILE": PROFILE_NAME,
                "UV_CACHE_DIR": str(
                    Path(environment.get("XDG_CACHE_HOME", home / ".cache"))
                    / "nebius"
                    / "uv"
                ),
                "PATH": f"{cli_path().parent}:/usr/local/bin:/usr/bin:/bin",
            }
        )
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=environment,
        )
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stderr_lines: list[str] = []
        self.next_id = 1
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for raw_line in self.process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                self.messages.put(value)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for raw_line in self.process.stderr:
            self.stderr_lines.append(raw_line.rstrip())
            del self.stderr_lines[:-30]

    def _send(self, value: dict[str, Any]) -> None:
        if self.process.poll() is not None:
            detail = "\n".join(self.stderr_lines[-8:])
            raise BridgeError(f"Nebius MCP stopped before the request completed.\n{detail}")
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def request(self, method: str, params: dict[str, Any], *, timeout: float = 60) -> Any:
        request_id = self.next_id
        self.next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = "\n".join(self.stderr_lines[-8:])
                raise BridgeError(f"Timed out waiting for {method}.\n{detail}")
            if self.process.poll() is not None and self.messages.empty():
                detail = "\n".join(self.stderr_lines[-8:])
                raise BridgeError(f"Nebius MCP stopped during {method}.\n{detail}")
            try:
                message = self.messages.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise BridgeError(f"MCP error during {method}: {message['error']}")
            return message.get("result")

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": VERSION},
            },
            timeout=180,
        )
        self.notify("notifications/initialized")

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)


def health(*, refresh: bool) -> dict[str, Any]:
    client = McpClient(refresh=refresh)
    try:
        client.initialize()
        result = client.request("tools/list", {}, timeout=60)
        tools = result.get("tools", []) if isinstance(result, dict) else []
        names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
        missing = EXPECTED_TOOLS - names
        if missing:
            raise BridgeError("Pinned MCP is missing expected tools: " + ", ".join(sorted(missing)))
        return {"ok": True, "commit": MCP_COMMIT, "tools": sorted(EXPECTED_TOOLS)}
    finally:
        client.close()


def profile_parent_id() -> str:
    config_path = Path.home() / ".nebius" / "config.yaml"
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise BridgeError(f"Could not read the Nebius CLI configuration: {error}") from error

    in_profile = False
    for line in lines:
        if line == f"    {PROFILE_NAME}:":
            in_profile = True
            continue
        if in_profile and line.startswith("    ") and not line.startswith("        "):
            break
        if in_profile and line.startswith("        parent-id:"):
            parent_id = line.split(":", 1)[1].strip()
            if re.fullmatch(r"project-[a-z0-9]+", parent_id):
                return parent_id
            break
    raise BridgeError(f"Profile {PROFILE_NAME!r} has no valid project parent ID")


def call_action(action: str) -> Any:
    tool_name, arguments = FIXED_ACTIONS[action]
    if action == "projects":
        project_id = profile_parent_id()
        arguments = {
            "command": f"nebius iam project get {project_id} --format json --no-progress"
        }
    client = McpClient()
    try:
        client.initialize()
        return client.request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=90,
        )
    finally:
        client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    health_parser = subparsers.add_parser("health", help="Verify the pinned MCP handshake and tools")
    health_parser.add_argument("--refresh", action="store_true", help="Refresh the pinned uv package first")
    call_parser = subparsers.add_parser("call", help="Run one fixed, read-only bridge action")
    call_parser.add_argument("action", choices=sorted(FIXED_ACTIONS))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = health(refresh=args.refresh) if args.command == "health" else call_action(args.action)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except (BridgeError, OSError, subprocess.SubprocessError) as error:
        print(f"nebius bridge: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
