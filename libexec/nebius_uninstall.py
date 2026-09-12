"""Agent access to the same local-only uninstaller used by the panel."""

from pathlib import Path
import json
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/nebius-uninstall"
WARNING = (
    "This removes the Omarchy widget, Nebius agent tools, dedicated browser profile, "
    "saved port forwards and local plugin state. Cloud VMs, disks, projects and networks "
    "are not stopped or deleted and may continue to incur charges. Removing the dedicated "
    "SSH key can make existing VMs inaccessible. Shared CLI and uv tools are kept by default."
)


class UninstallError(RuntimeError):
    pass


def check_agent_configs(*, verify=False):
    """Do not silently skip a registration when an agent CLI is missing/broken."""
    home = Path.home()
    expected = str(home / ".config/omarchy/plugins/nebius/libexec/nebius_agent_mcp.py")
    for command, path, key in (("codex", home / ".codex/config.toml", "mcp_servers"),
                               ("claude", home / ".claude.json", "mcpServers")):
        if not path.exists():
            continue
        try:
            text = path.read_text()
            parsed = tomllib.loads(text) if command == "codex" else json.loads(text)
            entry = parsed.get(key, {}).get("nebius", {})
        except (ValueError, OSError, AttributeError) as error:
            raise UninstallError(f"Cannot safely inspect {command} configuration; repair it before uninstalling.") from error
        if isinstance(entry, dict) and entry.get("command") == "python3" and entry.get("args") == [expected]:
            if verify:
                raise UninstallError(f"{command} still has this plugin's MCP registration. Cleanup is incomplete.")
            if not shutil.which(command):
                raise UninstallError(f"The {command} MCP registration remains but its CLI is unavailable. Restore {command} on PATH before uninstalling.")


def _run(arguments, timeout):
    try:
        return subprocess.run([str(SCRIPT), *arguments], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise UninstallError("Uninstall timed out. Do not report success; check the plugin and local state before retrying.") from error
    except OSError as error:
        raise UninstallError("Could not run the plugin uninstaller: " + str(error)) from error


def plan():
    result = _run(["--check"], 30)
    return {"ready": result.returncode == 0, "warning": WARNING,
            "defaults": {"keep_cli": True, "keep_ssh_key": True, "keep_uv": True},
            "details": (result.stdout + result.stderr).strip(),
            "instruction": "Explain the warning and ask whether to keep the CLI, SSH key and uv. Then obtain explicit uninstall approval. "
                           "Use uninstall_plugin, not bare omarchy plugin remove, agent MCP removal or deleting the plugin directory. "
                           "No Omarchy cleanup hook is required."}


def uninstall(*, confirmed, keep_cli, keep_ssh_key, keep_uv):
    if confirmed is not True or any(type(value) is not bool for value in (keep_cli, keep_ssh_key, keep_uv)):
        raise UninstallError("Uninstall requires explicit confirmation and boolean choices for the CLI, SSH key and uv.")
    if not keep_uv:
        raise UninstallError("Removing the shared uv package needs a visible terminal. Use U in the Nebius panel, or choose to keep uv.")
    arguments = ["--yes", "--keep-cli" if keep_cli else "--remove-cli",
                 "--keep-ssh-key" if keep_ssh_key else "--remove-ssh-key", "--keep-uv"]
    result = _run(arguments, 120)
    details = (result.stdout + result.stderr).strip()
    if result.returncode or "Local Nebius plugin setup removed." not in result.stdout:
        raise UninstallError("Uninstall did not complete. Do not report success.\n" + details)
    return {"status": "removed", "cloud_resources_changed": False, "details": details,
            "next_step": "Start a new agent session; this session may still display the old tool list."}


if __name__ == "__main__":
    try:
        if sys.argv[1:] not in (["check-agents"], ["verify-agents"]):
            raise UninstallError("Expected check-agents or verify-agents")
        check_agent_configs(verify=sys.argv[1] == "verify-agents")
    except UninstallError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
