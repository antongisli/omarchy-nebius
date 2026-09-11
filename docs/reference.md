# Setup, behavior and technical reference

Implementation details for [Nebius GPU for Omarchy](../README.md).

This repository does not depend on `other-tool` or modify its configuration.

## What setup does

- Installs Arch's official `uv` package through `omarchy-pkg-add uv` in a visible terminal.
- Installs Nebius CLI `0.12.269` into `~/.nebius/bin/nebius` after verifying the pinned Linux checksum.
- Creates the dedicated `omarchy-nebius-mcp` profile through the official `nebius profile create` browser flow.
- Rejects service-account profiles and paths under `~/.other-tool`; it never reuses the active `other-tool` profile.
- Uses the selected tenant without locking the plugin to one project. Projects created by the signed-in user are preferred; shared tenant projects are hidden from normal placement.
- Downloads and tests Nebius MCP commit `6388bf779acdd331d9b2016230b37f8bf7177e12` in a plugin-private uv cache.
- Creates a dedicated Ed25519 key at `~/.ssh/nebius-ed25519` for plugin-created VMs.
- Detects Omarchy's configured agent and registers a constrained `nebius` MCP server with each installed supported agent: Codex and Claude Code. It exposes typed capacity, project setup, plan, create, list, start, stop, delete, and connect tools—never a free-form command executor.
- Keeps the upstream MCP runtime private to its narrow compatibility bridge.
- Uses a normal tiled setup terminal so browser authentication never leaves a blocking floating overlay.

The installer is re-runnable and does not edit `.zshrc`. It uses each agent's own CLI to add only the `nebius` MCP registration to `~/.codex/config.toml` and/or `~/.claude.json`, and refuses to overwrite a server with the same name but a different command. Agent installation is not required for the keyboard manager; it is required only for natural-language actions. Setup reports when neither supported agent is installed and reports sign-in separately for every agent it finds.

## Default VM workflow

- Reads regional Capacity Advisor data for the configured tenant while keeping shared project names out of the normal workflow.
- Opens from the last successful capacity snapshot immediately; `R` requests a live refresh and falls back visibly if Nebius times out.
- Lets the user choose the GPU type and region first, then uses only projects created by that user (plus the preferred profile project and plugin-created projects).
- If that region has no personal project, proposes the editable name `gpu-<region>`, with Nebius's default network and subnet, before a normal confirmation. A project can hold any Nebius resources.
- Press `P` to toggle on-demand/preemptible and edit the proposed VM name.
- Checks regional SSD quota before project creation. VM review and confirmed creation both run live, read-only preflight: project-specific platform/preemptible eligibility, GPU preset/count, capacity advice, READY subnet, boot-image readiness/size, name conflicts, SSD quota and regular PAYG GPU quota. Critical unavailable checks block VM allocation. CLI JSON validation still runs before allocating a disk.
- Preflight is not a reservation, a complete CPU/network quota audit, or proof of create permissions. Unreported capacity is disclosed. Regular PAYG GPU quotas are not applied to preemptibles. New-project placement cannot verify project-specific eligibility until the project exists; that project remains if later VM checks fail.
- Shows the exact configuration, allocation, 200 GiB boot disk, price estimate and manual-stop reminder before creation. Small terminals page through all billing terms before confirmation is enabled. Escape returns to editing.
- Uses Ubuntu 24.04 with CUDA 13.0 and the plugin's dedicated SSH key.
- Auto-stop was removed in v0.5.4. No timer is installed on create, start or recovery. Stop VMs manually when finished. Old plans that promised auto-stop must be reviewed again; obsolete automatic-stop invocations are harmless no-ops.
- Never deletes automatically. Deletion uses a clear destructive confirmation and removes the plugin-created boot disk too.
- Shows stage and elapsed time in a persistent terminal. `B` returns to the overview; a detached worker continues even if the terminal closes. `A` follows progress and shows the result.
- Shows existing VMs in personal projects. External VMs use your SSH keys/agent and may need a login username; a private-only address needs a network route or VPN. Cloud visibility does not guarantee SSH access.
- Keeps uncertain creates as **Launch unconfirmed** requests, separately below actual VMs, not as VM health states. Recheck to restore the exact request-labelled VM's management and SSH access. Recovery does not schedule a stop.
- CLI JSON rejections and the specifically recognized server preemptible-eligibility rejection are different from timeouts. **Check for a rejected request** verifies the recorded failure and live disk ownership/attachments, clears only that false recovery block, and preserves the disk as **Boot disk available**. The next compatible launch in that project reviews and reuses the disk; no extra SSD quota or second boot disk is needed. Other remote errors remain uncertain.
- If recovery remains uncertain, inspect the project in the Nebius console. Archiving requires explicit confirmation and only removes the local duplicate-launch guard; it does not clean up any billable resources.
- Failed boot-disk deletion stays visible as **disk remains**, with a cleanup retry action. Delete is limited to plugin-registered resources.
- Select a saved boot disk to reuse, inspect, or permanently delete it. Cleanup requires explicit confirmation and live checks for ownership, attachments (including stopped VMs), locks, readiness and deletion protection. Nothing is deleted automatically.
- VM actions include **Disks and storage**, manual start/stop and confirmed deletion.
- The bar widget shows a small badge over the Nebius symbol: `0`–`9`, then `9+`, with the exact running VM count in the tooltip. It covers the same visible personal projects as **Your VMs**, including pre-existing VMs. Stopped VMs, failed requests and saved disks are not counted. Before setup completes, the badge is hidden and no VM polling runs. After setup, background refresh runs about every 30 seconds while connected; unknown, partial, failed or older-than-90-second results display `?`, never a false zero. Expired sessions retain the `?` badge with reconnect guidance. `R` refreshes it immediately.

Stopped VMs stop incurring compute charges, but their disks remain billable until deleted.

Estimates use [official Nebius pricing](https://docs.nebius.com/compute/resources/pricing), checked September 9, 2026; they exclude traffic and taxes and are not quotes. Quotas and eligibility can change; preflight reads them again for each confirmed launch. Nebius identifies supported preemptible platforms through [`allowed_for_preemptibles`](https://docs.nebius.com/compute/virtual-machines/preemptible), not through regional capacity counts.

## Install

```bash
omarchy plugin add https://github.com/antongisli/omarchy-nebius --enable
```

Omarchy clones and enables the plugin but intentionally runs no install hook. Open the **Nebius** bar icon and select **Set up Nebius**. The keyboard manager does not require an agent.

Optional global shortcuts are provided in [config/keybindings.lua](../config/keybindings.lua):

| Shortcut | Action |
| --- | --- |
| Super+Ctrl+G | Get a GPU VM |
| Super+Ctrl+J | Jump into a running VM |
| Super+Ctrl+M | Open the manager |

They are not automatically installed elsewhere; check `omarchy menu keybindings --print` before adding them to your user `bindings.lua`. Existing shortcuts and packaged defaults are preserved.

Omarchy's numbered panel shortcuts depend on the widget's position in your bar. Use the icon or the explicit G/J/M bindings for a stable destination.

Use arrows or `j/k`, Enter, Escape/back, and `/` search throughout the terminal. Menus separate section headings, bold choices, and indented descriptions, with a full-width selection highlight and space between choices. `Home` / `End` select the first / last item; Page Up / Page Down scroll by a page. Press `?` for the selected item's complete text and all keyboard shortcuts. Long resource details wrap; review and JSON details preserve indentation. From the manager or N panel:

- `C` — view current GPU capacity.
- `G` — choose a GPU family, select its region/configuration, choose project placement, review, and confirm creation.
- `Shift+J` — search running VMs and connect (lowercase `j` moves down).
- `V` — overview, connect, start/stop; explicitly delete plugin-owned VMs.
- In **Your VMs**, Enter opens the selected resource's actions. In **Jump**, Enter connects and `M` opens actions for the highlighted VM. Deletion defaults to Cancel and pages through the exact resources/data-loss warning before final confirmation. Secondary disks are kept.
- `A` — progress, result and full diagnostic details.
- `S` — account/reconnect.
- `R` — refresh the current capacity/inventory view, or panel status.
- `U` — **Uninstall Nebius plugin** and its local setup. The confirmation explicitly warns that cloud resources remain unchanged and may continue to incur charges.

For natural-language use, choose Codex or Claude Code in Omarchy, sign in to that agent, and start a new agent session to load the updated tools. Ask “show GPU capacity” or “get me a VM and put me in it.” The agent presents GPU choices before project placement, shows the plan, asks for a normal confirmation, and relies on write-tool approval instead of a typed magic phrase.

## Development smoke test

```bash
omarchy plugin validate .
bash -n bin/*
python3 -m py_compile libexec/*.py
python3 -m unittest discover -s tests -v
```

The setup status can be inspected without opening the panel:

```bash
bin/nebius-status --probe --json | jq
```

## Omarchy marketplace

The repository layout is compatible with Omarchy Quattro and the official marketplace: one root `manifest.json`, README, MIT license, and `preview.png`. The intended listing metadata is **Developer Tools** with the `ai`, `bar`, and `launcher` tags.

A local run of the official static analyzer found no blocking patterns in v0.6.0. Installer and privilege capabilities required review. This is development evidence, not marketplace approval: the published commit needs a fresh official scan and maintainer review. No marketplace submission has been made.

## Security boundary

The upstream beta MCP has a general command executor. This plugin pins every upstream MCP subprocess to the dedicated `omarchy-nebius-mcp` browser-auth profile and never forwards user-authored command strings to it. The agent-facing bridge accepts only typed arguments and calls predefined operations. Creation requires a fresh ten-minute plan plus write-tool approval. Start/stop/connect validate personal-project access; deletion additionally requires plugin registration, an ownership label and explicit destructive confirmation.

Tests use temporary state and synthetic CLI responses, with no cloud mutations. Live validation covers discovery, storage quota, plan review, keyboard navigation and native rendering. Provisioning and SSH into a newly created VM still need a separate, user-confirmed billable test.

Since v0.5.1, confirmed creation validates the complete instance JSON with the real pinned CLI **before allocating a disk**. Validation runs in an unprivileged user/network namespace using Arch's `/usr/bin/unshare`; networking is disabled, and there is no unsandboxed fallback. If user namespaces are unavailable or validation cannot be established, creation stops before allocation. JSON enum names use the protobuf spelling (`READ_WRITE`, `STOP`, `FAIL`, `FORBID`), not the lowercase spellings used by CLI flags.

To run the real CLI parser regressions on Omarchy, including rejection of the original bad values:

```bash
NEBIUS_TEST_OFFLINE_CLI=1 python3 -m unittest discover -s tests -v
```

State lives under `~/.local/state/nebius/`: `operation.json`, `jobs/`, `activity.log`, `pending/`, `resolved-requests/`, `reusable-disks/`, `archived-requests/`, VM registry, capacity/inventory snapshots and SSH connection settings. Resolved failures retain their original request and error evidence. Read-only operations can be canceled with Escape; closing a UI does not cancel an already submitted mutation.

## Uninstall and reinstall

Use `U  Uninstall Nebius plugin` in the N panel for a complete local reset. It opens a normal tiled terminal and defaults to **Cancel**. Before confirmation it states that Nebius cloud resources are outside the uninstall boundary: VMs, disks, projects, networks, and other resources are left exactly as they are and may continue to incur charges.

Confirmed uninstall always removes the Omarchy widget/plugin, local state and private MCP cache, the dedicated `omarchy-nebius-mcp` profile and browser token, exact plugin-owned Codex and Claude Code MCP registrations, the marked shortcut block, and Arch `uv` when setup proves this plugin installed it. Before the final confirmation, separate keyboard choices ask whether to keep the Nebius CLI and dedicated SSH key; both default to **Keep**. Keeping the CLI makes reinstall faster. Keeping the key preserves access to existing VMs that trust it—a replacement key will not unlock those machines. A CLI is removable only when setup proves ownership and its checksum still matches; a changed binary is preserved. Pre-existing profiles, unrelated MCP servers, keys, shortcuts, packages, and the separate `other-tool` integration are untouched.

Running `omarchy plugin remove nebius` directly only disables/removes the shell bundle; Omarchy has no plugin uninstall hook, so setup artifacts would remain and setup would not behave like a first install. After using the plugin's `U` action, reinstall with the command in the [README](../README.md#install). To exercise every dependency step, choose to remove both the CLI and SSH key; if kept, setup recognizes and reuses them. Start a new Codex or Claude Code session after reinstalling so it loads the new MCP registration.
