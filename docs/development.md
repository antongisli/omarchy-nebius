# Development

The plugin is an Omarchy Quattro bar widget plus a Python terminal UI. It uses the official Nebius CLI for constrained cloud operations and an agent-facing MCP bridge shared by Codex and Claude Code.

## Checks

Run from the repository root with Python 3.11+:

```bash
for script in bin/* tools/capture-previews; do bash -n "$script"; done
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

`compute … --async` returns an operation ID, not necessarily a JSON document, even with `--format json` ([CLI reference](https://docs.nebius.com/cli/reference/compute/instance/start)). The lifecycle regression tests pass raw stdout through the real CLI wrapper before polling; mocking `run_cli` with only a parsed dictionary would miss this contract. Unknown submission replies must keep the journal and must never trigger an automatic resubmission.

## Layout

| Path | Responsibility |
| --- | --- |
| `manifest.json`, `qml/` | Omarchy widget, live count, launcher and reconnect state |
| `bin/` | Setup, status, uninstall and terminal entry points |
| `libexec/nebius_ui.py` | Keyboard menus and operation views |
| `libexec/nebius_core.py` | Preflight, resource lifecycle and persistent outcomes |
| `libexec/nebius_runtime.py` | Shared release version and private/legacy CLI selection |
| `libexec/nebius_ports.py`, `nebius_ssh.py` | Persistent SSH tunnels and interactive login readiness |
| `libexec/nebius_jobs.py`, `nebius_job.py` | Concurrent work, durable progress and results |
| `libexec/nebius_agent_mcp.py` | Typed agent tools |
| `libexec/nebius_*_mcp.py` | Per-agent registration helpers |
| `assets/`, `tools/` | Brand assets and reproducible public previews |
| `tests/` | Resource-boundary, failure and terminal-layout regressions |

## Contributing

### Reproduce documentation screenshots

On Omarchy, run `bash tools/capture-previews`. It renders the production QML launcher and terminal drawing code with synthetic data, using Qt's offscreen backend. It updates `preview.png`, `assets/terminal-preview.svg`, four terminal screens in `docs/screenshots/` and an additional native launcher capture; no desktop window opens and no cloud resources are read or changed. Review the resulting images before committing them. The popup host is capture-only because offscreen Qt cannot create a Wayland popup.

Without Omarchy, the terminal screenshots and cover can also be rasterized from the same SVG drawing code using a renderer such as resvg. With CairoSVG and its system `libcairo` library installed, PNG output is available directly: `uv run --no-project --with cairosvg==2.7.1 python tools/render_preview.py --surface cover --output preview.png`. Choose `port-form`, `ports`, `overview` or `activity`, with `--width 80 --height 26`, for the individual screenshots. This does not capture or validate the native QML launcher. These are documentation build tools, not plugin dependencies.

For a narrow-terminal check without Qt, run `python3 tools/render_preview.py --surface port-form --width 48 --height 20 --output /tmp/nebius-port-form.svg` and inspect the SVG.

### Release checks

Required removal check: verify `U · Uninstall Nebius plugin`, direct script and agent removal on **unmodified Omarchy without cleanup hooks**, including cancellation, retained dependencies, failure reporting and a clean setup on reinstall. Also verify cleanup from a separate checkout after bare native removal has already deleted the bundle. Omarchy PR #11470 is optional; acceptance is not a release gate. Documentation must clearly direct users and agents to full uninstall rather than claiming the old generic command cleans external setup.

CI pins unmodified Omarchy commit `31bd80daa4613ffdee995ac27467fce5a2990806` and runs the standalone panel, agent and recovery integration cases. To run them locally on Linux (real commands, temporary home, fake IPC/services; no desktop or cloud changes):

```bash
NEBIUS_TEST_OMARCHY_LEGACY_SOURCE=/path/to/unmodified-omarchy python3 -m unittest discover -s tests -p test_omarchy_cleanup.py -v
```

This suite rejects a host with `--skip-cleanup` so it cannot accidentally pass by relying on the proposed hook. It covers interactive confirmation/cancellation, agent self-removal, current-host backup behavior and retryable recovery. Real shell IPC and fresh setup still need a separately approved smoke test; mocked IPC is not graphical acceptance.

Optional additional compatibility test for the proposed upstream hook:

```bash
NEBIUS_TEST_OMARCHY_SOURCE=/path/to/omarchy-with-cleanup-hook python3 -m unittest discover -s tests -p test_omarchy_cleanup.py -v
```

1. Run the suite, shell syntax checks, Omarchy manifest validation and offscreen widget checks.
2. Verify setup with no CLI, a matching borrowed CLI, another CLI version, a failed checksum and repair/reinstall. Installer-stage tests isolate these cases without changing the account.
3. Rebuild screenshots and check README links, version metadata, dependency pins and removal instructions.
4. Push the release candidate, wait for GitHub checks, and validate a fresh GitHub clone on Omarchy.
5. A full first-install/browser-auth/new-VM/SSH/forward/stop/delete smoke test requires the owner's participation and explicit billable/destructive confirmations. Never run it against existing resources without permission.
6. Tag the checked commit for a beta release. Marketplace submission is separate: exact-commit validation, rights confirmation and maintainer approval are required.

### Widget updates

When releasing a widget change, move its QML entry point to a new versioned directory and update the manifest, offscreen test and preview imports. Omarchy 4.0.2 can retain a cached component at the old URL after `omarchy plugin update`; a new entry-point path loads the update without restarting the desktop shell.

Open an issue or pull request with the behavior you want to change and its user-facing reason. Include reproduction steps for bugs. Keep credentials, account identifiers and private resource data out of issues and captures. Test consequential changes with synthetic responses before a live cloud trial.

Use the system and terminal fonts, and retain arrows, j/k, Enter and Escape throughout. Brand accents must remain readable without changing the user's terminal palette.
