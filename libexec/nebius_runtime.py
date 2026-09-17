"""Shared release metadata and CLI locations; never replace a user's CLI."""
import json
import os
from pathlib import Path
import sys

CLI_VERSION = "0.12.277"
PREVIOUS_CLI_VERSION = "0.12.269"
VERSION = json.loads((Path(__file__).resolve().parents[1] / "manifest.json").read_text())["version"]
CLIENT_NAME = "omarchy-nebius"
USER_AGENT_PREFIX = f"{CLIENT_NAME}/{VERSION}"


def cli_environment():
    """Identify only plugin-owned calls; never mutate the caller's environment."""
    return {**os.environ, "NEBIUS_CLI_USER_AGENT_PREFIX": USER_AGENT_PREFIX}


def private_cli_path(version=CLI_VERSION):
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "nebius/cli" / version / "nebius"


def cli_path():
    if os.environ.get("NEBIUS_CLI_BIN"):
        return Path(os.environ["NEBIUS_CLI_BIN"])
    private = private_cli_path()
    previous = private_cli_path(PREVIOUS_CLI_VERSION)
    legacy = Path.home() / ".nebius/bin/nebius"
    if private.exists() or private.is_symlink():
        return private
    # Keep existing installations usable until the user runs setup to upgrade.
    if previous.exists() or previous.is_symlink():
        return previous
    return legacy if legacy.exists() else private


if __name__ == "__main__":
    if sys.argv[1:] == ["user-agent"]:
        print(USER_AGENT_PREFIX)
    elif sys.argv[1:] == ["previous-cli"]:
        print(private_cli_path(PREVIOUS_CLI_VERSION))
    else:
        print(private_cli_path() if sys.argv[1:] == ["private-cli"] else cli_path())
