# Development

The plugin is an Omarchy Quattro bar widget plus a Python terminal UI. It uses the official Nebius CLI for constrained cloud operations and an agent-facing MCP bridge shared by Codex and Claude Code.

## Checks

Run from the repository root with Python 3.11+:

```bash
bash -n bin/*
python3 -m compileall -q libexec tests tools
python3 -m unittest discover -s tests -v
```

The tests use temporary directories and synthetic cloud responses. They do not provision cloud resources. Shell tests also need `jq`; Linux provides `flock` through util-linux.

On Omarchy, validate the shell integration:

```bash
omarchy plugin validate .
```

With the pinned Nebius CLI already installed on Linux, opt into request-parser checks in an isolated network namespace:

```bash
NEBIUS_TEST_OFFLINE_CLI=1 python3 -m unittest discover -s tests -v
```

Those three checks skip elsewhere. A successful unit-test run is not a live provisioning test. Test actual creation, SSH and deletion separately in a project you control, with explicit review of the billable resources.

## Layout

| Path | Responsibility |
| --- | --- |
| `manifest.json`, `qml/` | Omarchy widget, live count, launcher and reconnect state |
| `bin/` | Setup, status, uninstall and terminal entry points |
| `libexec/nebius_ui.py` | Keyboard menus and operation views |
| `libexec/nebius_core.py` | Preflight, resource lifecycle and persistent outcomes |
| `libexec/nebius_agent_mcp.py` | Typed agent tools |
| `libexec/nebius_*_mcp.py` | Per-agent registration helpers |
| `assets/`, `tools/` | Brand assets and reproducible public previews |
| `tests/` | Resource-boundary, failure and terminal-layout regressions |

## Contributing

When releasing a widget change, move its QML entry point to a new versioned directory and update the manifest, offscreen test and preview imports. Omarchy 4.0.2 can retain a cached component at the old URL after `omarchy plugin update`; a new entry-point path loads the update without restarting the desktop shell.

Open an issue or pull request with the behavior you want to change and its user-facing reason. Include reproduction steps for bugs. Keep credentials, account identifiers and private resource data out of issues and captures. Test consequential changes with synthetic responses before a live cloud trial.

Use the system and terminal fonts, and retain arrows, j/k, Enter and Escape throughout. Brand accents must remain readable without changing the user's terminal palette. See [DESIGN.md](../DESIGN.md).
