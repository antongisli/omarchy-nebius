"""Shared release metadata and CLI locations; never replace a user's CLI."""
import json
import os
from pathlib import Path
import sys

CLI_VERSION = "0.12.269"
VERSION = json.loads((Path(__file__).resolve().parents[1] / "manifest.json").read_text())["version"]


def private_cli_path():
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "nebius/cli" / CLI_VERSION / "nebius"


def cli_path():
    if os.environ.get("NEBIUS_CLI_BIN"):
        return Path(os.environ["NEBIUS_CLI_BIN"])
    private = private_cli_path()
    legacy = Path.home() / ".nebius/bin/nebius"
    return private if private.exists() or private.is_symlink() or not legacy.exists() else legacy


if __name__ == "__main__":
    print(private_cli_path() if sys.argv[1:] == ["private-cli"] else cli_path())
