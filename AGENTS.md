# Nebius Omarchy plugin

This is the independent Nebius GPU plugin for Omarchy. It does not depend on or modify `other-tool`. Preserve unrelated user changes and cloud resources.

Treat every tracked file and commit message as public. Product-status claims must be supported by public documentation or reproducible behavior from released public tools. Omit uncertain roadmap claims.

## Removing the installed plugin

- Use the Nebius MCP tools `plan_plugin_uninstall` and `uninstall_plugin` when available. They share the panel's uninstaller and work without an Omarchy cleanup hook.
- Otherwise run `~/.config/omarchy/plugins/nebius/bin/nebius-uninstall --check` first. Explain that cloud resources remain unchanged and may keep costing money. Ask whether to keep the underlying CLI, dedicated SSH key and shared uv package; keeping them is the default. The plugin-managed `~/.local/bin/nebius` link is removed either way and restored by setup on reinstall. Removing the key can prevent access to existing VMs.
- After explicit approval, run that script with `--yes` and the agreed `--keep-cli`/`--remove-cli` and `--keep-ssh-key`/`--remove-ssh-key` flags. Use `--keep-uv` for unattended runs; package removal needs the user's visible terminal. Never run silent privilege escalation.
- Do not substitute bare `omarchy plugin remove nebius`, deleting the directory or removing only its MCP registration. Current Omarchy does not invoke cleanup hooks, so these leave external setup behind.
- If the installed bundle is already gone, run `bin/nebius-uninstall` from a checkout of this repository to finish cleanup. Do not run setup or create cloud resources first.
- Report success only on exit code 0 **and** `Local Nebius plugin setup removed.` in the output. Cancellation, timeouts and partial cleanup are not success. Never delete cloud VMs, disks or projects as part of uninstall.

## Development checks

Use Python 3.11+ and run `python3 -m unittest discover -s tests -v`. Run `bash -n` on changed shell scripts. See `docs/development.md` for Linux integration checks against unmodified Omarchy. Tests must use temporary homes and fake cloud responses, not the user's account or desktop.
