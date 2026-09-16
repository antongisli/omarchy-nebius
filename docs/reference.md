# Setup, behavior and technical reference

Implementation details for [Nebius GPU for Omarchy](../README.md).

The plugin uses a dedicated profile and does not modify unrelated integrations.

## What setup does

- Installs Arch's official `uv` package through `omarchy-pkg-add uv` in a visible terminal.
- Reuses an existing matching Nebius CLI without taking ownership. Otherwise installs the checksum-verified `0.12.269` binary privately at `${XDG_DATA_HOME:-~/.local/share}/nebius/cli/0.12.269/nebius`. Existing `~/.nebius/bin/nebius` binaries are never overwritten, including newer or older versions. Unexpected files at the private destination stop setup with a repair message. An exact-path installation receipt distinguishes owned and borrowed CLIs. When the terminal command is free, setup adds a plugin-owned `~/.local/bin/nebius` link so `nebius` works in regular Omarchy terminals. Existing commands and paths are preserved.
- Creates the dedicated `omarchy-nebius-mcp` profile through the official `nebius profile create` browser flow.
- Rejects service-account profiles and uses only its dedicated browser-auth profile.
- Uses the selected tenant without locking the plugin to one project. Projects created by the signed-in user are preferred; shared tenant projects are hidden from normal placement.
- Downloads and tests Nebius MCP commit `6388bf779acdd331d9b2016230b37f8bf7177e12` in a plugin-private uv cache. It requires Python 3.13+; uv provisions the runtime environment.
- Creates a dedicated Ed25519 key at `~/.ssh/nebius-ed25519` for plugin-created VMs.
- Detects Omarchy's configured agent and registers a constrained `nebius` MCP server with each installed supported agent: Codex and Claude Code. It exposes typed capacity, project setup, plan, create, list, start, stop, delete, and connect tools—never a free-form command executor.
- Keeps the upstream MCP runtime private to its narrow compatibility bridge.
- Uses a normal tiled setup terminal so browser authentication never leaves a blocking floating overlay.

The installer is re-runnable and does not edit shell profiles. It uses each agent's own CLI to add only the `nebius` MCP registration to `~/.codex/config.toml` and/or `~/.claude.json`, and refuses to overwrite a server with the same name but a different command. Agent installation is not required for the keyboard manager; it is required only for natural-language actions. Setup reports when neither supported agent is installed and reports sign-in separately for every agent it finds.

Omarchy supplies the ordinary terminal tools used by setup: Python, Bash, jq, curl, OpenSSH, fzf, gum and util-linux. Package installation runs visibly and may ask for your password; the plugin does not silently escalate privileges.

## Image selection

After choosing a GPU configuration and destination project, open **Boot image**
in VM settings. The picker lists public images for the selected region and
custom images in accessible projects of the configured tenant, including shared
image projects. This does not add shared projects to the VM destination list.
Search by name or image ID; press R to refresh. Listing failures are shown as
incomplete discovery, while images from successful sources remain usable.

Images must be READY and not reconciling. Known CPU-architecture mismatches,
unsupported platforms, and unsupported presets are excluded. Current documented
GPU platforms use AMD64 CPUs. Unknown architecture or absent GPU recommendations
are shown explicitly; lack of a recommendation is not treated as an exclusion.
An image must support cloud-init so the plugin can create the `dev` user and
install the SSH key. GPU driver and workload compatibility cannot be proven from
image metadata alone.

Selecting an image pins its exact ID for this launch. Boot disk size grows to at
least the image minimum and can be edited separately. Plans and final review
show the source, disk size and price estimate. Live preflight rechecks access,
region, compatibility, readiness and disk fit before allocation. Saved boot disks
are reused only when their image ID/family and disk size match. The selection is
local to the current launch; existing VMs are unaffected.

For agents, `list_images` accepts offering IDs and a destination project ID;
`plan_gpu_vm` accepts optional `image_id` and `disk_gib`. The CLI equivalents are
`nebius_core.py images --offering-id ID --project-id ID` and
`nebius_core.py plan --offering-id ID --project-id ID --image-id IMAGE_ID --disk-gib 256`.
The selector uses images already present in Compute. It creates no image,
import, bucket or publishing configuration.

## GPU variant grouping

Known RTX PRO 6000, L40S and B200 variants share product names. Equivalent
configurations are grouped by tenant, region, GPU count, CPU count, RAM, GPU
memory and CPU architecture. Availability shows the best reported pool, not a
sum across potentially overlapping capacity advice. Actual platform, preset and
fabric IDs are retained for placement, live preflight, pricing and API requests.
After project/image selection, the plugin picks a matching variant by capacity,
then estimated price on ties. Known project-level preemptible restrictions are
respected. A failure after submission never triggers a second automatic launch.

## Default VM workflow

- Reads regional Capacity Advisor data for the configured tenant while keeping shared project names out of the normal workflow.
- Opens from the last successful capacity snapshot immediately; `R` requests a live refresh and falls back visibly if Nebius times out.
- Lets the user choose the GPU type and region first, then uses only projects created by that user (plus the preferred profile project and plugin-created projects).
- If that region has no personal project, proposes the editable name `gpu-<region>`, with Nebius's default network and subnet, before a normal confirmation. A project can hold any Nebius resources.
- Press `P` to toggle on-demand/preemptible. Edit the proposed VM name in VM settings.
- Checks regional SSD quota before project creation. VM review and confirmed creation both run live, read-only preflight: project-specific platform/preemptible eligibility, GPU preset/count, capacity advice, READY subnet, boot-image readiness/size, name conflicts, SSD quota and regular PAYG GPU quota. Critical unavailable checks block VM allocation. CLI JSON validation still runs before allocating a disk.
- Preflight is not a reservation, a complete CPU/network quota audit, or proof of create permissions. Unreported capacity is disclosed. Regular PAYG GPU quotas are not applied to preemptibles. New-project placement cannot verify project-specific eligibility until the project exists; that project remains if later VM checks fail.
- Shows the exact configuration, allocation, selected boot disk size, price estimate and manual-stop reminder before creation. The boot disk defaults to 200 GiB and grows to fit the selected image. Small terminals page through all billing terms before confirmation is enabled. Escape returns to editing.
- Defaults to Ubuntu 24.04 with CUDA 13.0; **Boot image** offers public and custom images. New VMs use the plugin's dedicated SSH key.
- Auto-stop was removed in v0.5.4. No timer is installed on create, start or recovery. Stop VMs manually when finished. Old plans that promised auto-stop must be reviewed again; obsolete automatic-stop invocations are harmless no-ops.
- Never deletes automatically. Deletion requires explicit confirmation, verifies the signed-in user as the VM creator through cloud audit history, and removes only the exact reviewed boot disk after safety checks. Secondary disks are kept.
- Launch and start complete only after an authenticated SSH probe succeeds. Per-stage durations and total time from submission to verified SSH are saved in each job and displayed on a persistent result screen. Press `C` to SSH; the report remains when you return. Reopen it through `A` Activity or VM actions → Launch timings. Older jobs explicitly show when timings were not recorded.
- Shows stage and elapsed time in a persistent terminal. `Esc` or `B` returns to the overview; a detached worker continues even if the terminal closes. `A` follows progress and shows the result.
- Shows existing VMs in personal projects. External VMs use your SSH keys/agent and may need a login username; a private-only address needs a network route or VPN. Cloud visibility does not guarantee SSH access.
- Keeps uncertain creates as **Launch unconfirmed** requests, separately below actual VMs, not as VM health states. Recheck to restore the exact request-labelled VM's management and SSH access. Recovery does not schedule a stop.
- CLI JSON rejections and the specifically recognized server preemptible-eligibility rejection are different from timeouts. **Check for a rejected request** verifies the recorded failure and live disk ownership/attachments, clears only that false recovery block, and preserves the disk as **Boot disk available**. The next compatible launch in that project reviews and reuses the disk; no extra SSD quota or second boot disk is needed. Other remote errors remain uncertain.
- If recovery remains uncertain, inspect the project in the Nebius console. Archiving requires explicit confirmation and only removes the local duplicate-launch guard; it does not clean up any billable resources.
- Failed boot-disk deletion stays visible as **disk remains**, with a cleanup retry action. VM deletion is limited to visible personal projects and verified creator ownership—not plugin registration. Missing or inaccessible creation audit history blocks deletion; use the Nebius console to resolve it.
- Select a saved boot disk to reuse, inspect, or permanently delete it. Cleanup requires explicit confirmation and live checks for ownership, attachments (including stopped VMs), locks, readiness and deletion protection. Nothing is deleted automatically.
- VM actions include **Disks and storage**, manual start/stop and confirmed deletion.
- The VM overview exposes `C` SSH, `P` SSH port forwarding, `S` stop, `T` start, and `D` review deletion for the highlighted VM. Selection is retained on return. Actions have visible letter keys; resource choices have number keys and search. Start/stop/delete use one review, with `S`/`T`/`D` confirmation after reading its terms and `Esc` cancellation. Enter pages through deletion terms but never deletes.
- SSH waits up to 120 seconds for a real noninteractive login before opening the session, retrying transient boot/network failures and managed-VM key setup. Interactive sessions open only after the same key/agent login succeeds in the probe. Password-only login is not verified by this workflow; configure an SSH key or use your own terminal. Host-key changes fail immediately. `Esc` cancels only the connection attempt. Failed or immediately closed sessions retain a retry screen and save the diagnostic in `ssh-last-error.json`; no VM is stopped.
- The bar widget shows a small badge over the Nebius symbol: `0`–`9`, then `9+`, with the exact running VM count in the tooltip. It covers the same visible personal projects as **Your VMs**, including pre-existing VMs. Stopped VMs, failed requests and saved disks are not counted. Before setup completes, the badge is hidden and no VM polling runs. After setup, background refresh runs about every 30 seconds while connected; unknown, partial, failed or older-than-90-second results display `?`, never a false zero. Expired sessions retain the `?` badge with reconnect guidance. `R` refreshes it immediately.

Stopped VMs stop incurring compute charges, but their disks remain billable until deleted.

Estimates use [official Nebius pricing](https://docs.nebius.com/compute/resources/pricing); they exclude traffic and taxes and are not quotes. Quotas and eligibility can change; preflight reads them again for each confirmed launch. Nebius identifies supported preemptible platforms through [`allowed_for_preemptibles`](https://docs.nebius.com/compute/virtual-machines/preemptible), not through regional capacity counts.

## Install

```bash
omarchy plugin add https://github.com/antongisli/omarchy-nebius --enable
```

Omarchy clones and enables the plugin but intentionally runs no install hook. Open the **Nebius** bar icon and select **Set up Nebius**. The keyboard manager does not require an agent.

Setup installs **Super+Ctrl+M → Open Nebius** if the key is free and no opening-shortcut choice has been saved. It preserves an existing or disabled choice. The shortcut targets the plugin by ID, not the widget's bar position. If occupied, setup leaves it alone and points to **Shift+K · Shortcuts**; account setup can still finish. Open those settings from the panel or manager, including before sign-in. Choose **E · Change opening shortcut**, use Tab or up/down between modifiers and key, left/right to choose modifiers, type a letter, number or function-key name such as `F12` (F1–F24), then Enter to save. Escape cancels without writing. Conflicts stay in the form; F2 shows complete error details. Save validates the Lua, reloads Hyprland and checks the live binding; failed verification restores the prior file unless a concurrent edit appeared, which is preserved and reported.

**D · Disable opening shortcut** keeps the bar icon and is remembered through setup/repair. When no shortcut is set, the same action reads **Keep shortcut disabled** and saves that opt-out after confirmation, so later setup does not add the default.

Only the marked Nebius block in `~/.config/hypr/bindings.lua` is managed. Other applications keep their bindings. Uninstall removes the marked block, reloads Hyprland and checks the result. Manually edited or symlinked binding files may require manual repair rather than automatic replacement.

Optional direct-action shortcuts are provided in [config/keybindings.lua](../config/keybindings.lua):

| Shortcut | Action |
| --- | --- |
| Super+Ctrl+G | Get a GPU VM |

They are not automatically installed elsewhere; check `omarchy menu keybindings --print` before adding them to your user `bindings.lua`. Existing shortcuts and packaged defaults are preserved.

Omarchy's Super+Ctrl+1–9 shortcuts address panels in the **right-hand** bar section. They do not target Nebius by name, and do not include a Nebius icon placed in the centre or left. Use the dedicated opening shortcut or icon instead.

Use arrows or `j/k`, Enter, Escape/back, and `/` search throughout the terminal. Menus separate section headings, bold choices, and indented descriptions, with a full-width selection highlight and space between choices. `Home` / `End` select the first / last item; Page Up / Page Down scroll by a page. Press `?` for the selected item's complete text and all keyboard shortcuts. Long resource details wrap; review and JSON details preserve indentation. From the manager or N panel:

- `C` — view current GPU capacity.
- `P` — SSH port forwarding in the launcher, manager or VM list. In capacity, configuration and VM settings, it toggles the highlighted **On-demand / Preemptible** switch instead. Capacity views update immediately from the same snapshot; use `R` for fresh data.
- `G` — choose a GPU family, select its region/configuration, choose project placement, review, and confirm creation.
- `V` — list VMs, connect, start/stop; review deletion of VMs you created in visible personal projects.
- In **Your VMs**, `C` connects directly to the highlighted running VM and Enter opens its actions. Deletion has one review: Enter pages through the exact resources/data-loss warning, `D` confirms only after all terms are visible, and Esc cancels. Secondary disks are kept.
- `A` — progress, result and full diagnostic details.
- `S` — account/reconnect.
- `E` — settings. Kubernetes worker nodes are hidden from VM listings, SSH choices and the running-VM badge by default. Toggle **Include Kubernetes nodes** to show them as cluster-managed resources; direct start, stop and deletion actions remain unavailable.
- `Shift+K` — change or disable the opening shortcut (lowercase `k` moves up).
- `R` — refresh the current capacity/inventory view, or panel status.
- `U` — **Uninstall Nebius plugin** and its local setup. The confirmation explicitly warns that cloud resources remain unchanged and may continue to incur charges.

For natural-language use, choose Codex or Claude Code in Omarchy, sign in to that agent, and start a new agent session to load the updated tools. Ask “show GPU capacity” or “get me a VM and put me in it.” The agent presents GPU choices before project placement, shows the plan, asks for a normal confirmation, and relies on write-tool approval instead of a typed magic phrase.

VM lists refresh local job state every two seconds and fetch cloud inventory asynchronously (every five seconds while work is active, otherwise every thirty seconds, and on operation completion). Rows show Creating, Starting, Stopping, Deleting, or Waiting for SSH as appropriate. Managed Kubernetes nodes are identified from Nebius node-group ownership and provider-managed metadata rather than their names. They are hidden by default; the inventory reports how many were omitted. Confirmed deletion removes the row immediately; failed operations retain the VM. Navigation and search remain intact during refresh. Failed reads keep the last known inventory and show retry feedback. Pressing the same lifecycle action during an active operation follows its existing job.

VM launches are independent. Press `Esc` or `B` to leave a launch running in the background, then use `G` to start another; `A` follows every active operation. Shared resources remain serialized, so two plans cannot claim the same reusable boot disk, and an abandoned or uncertain request still blocks unsafe retries in its project until recovered.

The **SSH ready** stage starts after the VM is observed running with an address and ends after a successful authenticated command. It includes remaining guest startup and connection preparation, as well as probe attempts; it is not solely SSH handshake time. During the first 30 seconds, probes use a two-second connection timeout and retry after half a second; later attempts allow five seconds and retry after two seconds. The saved launch details include `ssh_probe.preparation_seconds` and per-attempt durations and outcomes. Readiness still requires successful authentication.

## SSH after reinstall

Keep the dedicated SSH key when uninstalling to retain access to existing VMs. SSH and port forwarding offer that key again for VMs carrying this plugin's creation label, even without a local VM record. Your configured SSH agent remains available for these older VMs; unrelated VMs use their normal SSH configuration.

Connecting does not adopt a VM or grant deletion access. If the key was removed or replaced, the plugin cannot restore the old private key: use another authorized key or the VM's recovery procedure. Reinstalling never changes the keys already authorized on a VM.

## SSH port forwarding

Choose **P · SSH port forwarding → N · Add port**, then select a VM. From **Your VMs**, highlight a VM and press **P** to manage its ports directly.

Enter the application's remote TCP port and the local port in one form. The local value follows the remote value until customized, with an unprivileged alternative for remote ports below 1024. If the local port is occupied, choose another. **Tab / ↑↓** switches fields, **←→** moves the cursor, **Enter** saves and **Esc** cancels. The diagram shows `this computer → SSH → VM application`; validation keeps both values on screen.

For example, forwarding remote port 8188 to local port 8188 makes an HTTP app available at `http://127.0.0.1:8188`. Start the app on the VM, listening on its loopback interface. Docker apps must publish their port to the VM loopback interface too. Tunnels bind only to your computer's `127.0.0.1` and use encrypted SSH; no public application port, domain or HTTPS certificate is needed.

Enabled mappings persist through a systemd user service independently of the terminal window. They are restored after login and reconnect after sleep or network changes. Stopping a VM preserves mappings without starting the VM automatically; deleting it removes that VM's mappings, status and local tunnel logs. Requests in flight can fail during reconnect—refresh the browser afterward.

The Ports list updates every two seconds while preserving selection and search; **R** refreshes immediately. Open HTTP apps in the browser, copy an address, inspect errors, pause/resume or remove a mapping. **Connected** describes the SSH tunnel, not application health: the application still needs to be running on the remote port. See [Security boundary](#security-boundary) for service, authentication and networking details.

## Update

Run `omarchy plugin update nebius`, then close and reopen existing Nebius terminals. The widget reloads through its versioned entry point; no desktop restart is needed. Start new agent sessions to load updated tools. Use **S · Set up / reconnect** if prompted; your account, SSH key and saved forwards are reused. See the [release notes](../CHANGELOG.md).

## Development smoke test

```bash
omarchy plugin validate .
for script in bin/*; do bash -n "$script"; done
python3 -m py_compile libexec/*.py
python3 -m unittest discover -s tests -v
```

The setup status can be inspected without opening the panel:

```bash
bin/nebius-status --probe --json | jq
```

## Omarchy marketplace

The repository layout is compatible with Omarchy Quattro and the official marketplace: one root `manifest.json`, README, MIT license, and `preview.png`. The intended listing metadata is **Developer Tools** with the `ai`, `bar`, and `launcher` tags.

Every submitted snapshot needs a fresh exact-commit scan with the marketplace's scanner and explicit maintainer approval. Installer, privilege and service-management capabilities require review; a clean static scan is not a security certification or marketplace approval.

## Security boundary

The upstream beta MCP has a general command executor. This plugin pins every upstream MCP subprocess to the dedicated `omarchy-nebius-mcp` browser-auth profile and never forwards user-authored command strings to it. The agent-facing bridge accepts only typed arguments and calls predefined operations. Creation requires a fresh ten-minute plan plus write-tool approval. Start/stop/connect validate personal-project access; deletion additionally requires successful cloud creation audit evidence identifying the current tenant user as the VM creator, a review of the exact boot disk, and explicit destructive confirmation. Plugin registration and labels are not required. If creation audit history is missing or inaccessible, use the Nebius console to resolve ownership. Secondary disks are retained; protected or attached boot disks are never blindly removed. Interrupted cleanup is bound to the confirming account and tenant.

Tests use temporary state, synthetic CLI responses and isolated installer stages, with no cloud mutations. Validation on Omarchy includes the full regression suite, the real CLI parser inside a disabled network namespace, manifest validation and offscreen widget rendering. Live cloud operations require explicit user confirmation and are not inferred from unit tests or example screenshots.

Since v0.5.1, confirmed creation validates the complete instance JSON with the real pinned CLI **before allocating a disk**. Validation runs in an unprivileged user/network namespace using Arch's `/usr/bin/unshare`; networking is disabled, and there is no unsandboxed fallback. If user namespaces are unavailable or validation cannot be established, creation stops before allocation. JSON enum names use the protobuf spelling (`READ_WRITE`, `STOP`, `FAIL`, `FORBID`), not the lowercase spellings used by CLI flags.

To run the real CLI parser regressions on Omarchy, including rejection of the original bad values:

```bash
NEBIUS_TEST_OFFLINE_CLI=1 python3 -m unittest discover -s tests -v
```

State lives under `~/.local/state/nebius/`: `operation.json`, `jobs/`, `activity.log`, `pending/`, `resolved-requests/`, `reusable-disks/`, `archived-requests/`, VM registry, capacity/inventory snapshots and SSH connection settings. Resolved failures retain their original request and error evidence. Read-only operations can be canceled with Escape; closing a UI does not cancel an already submitted mutation.

Lifecycle jobs use per-VM locks and atomic registry updates under a short registry lock. Launch/recovery/project workflows retain their shared allocation lock; they do not block unrelated VM lifecycle jobs. Each worker owns a separate progress file under `jobs/`; `cloud-operations/` stores asynchronous lifecycle operation IDs before polling. Activity can resume interrupted lifecycle jobs. Unknown submission outcomes are reconciled against resource state without blindly replaying destructive requests.

`ports.json` stores selected VM IDs, usernames and TCP mappings. `nebius-ports.service`, installed under the user's systemd configuration on the first forward, restores enabled mappings after login. It supervises independent foreground SSH processes, binds only `127.0.0.1`, uses keepalives, retains host-key verification under a VM-ID alias, and refreshes VM addresses. `ports-status.json` records heartbeat and per-forward status; stale status is not presented as connected. Reconnection restores the tunnel, not existing TCP sessions or application processes. Port lists and pause/remove actions work without cloud authentication; adding ports and refreshing VM addresses require a valid Nebius session. The daemon can reconnect to a previously saved address while the cloud session is expired.

New VM requests attach a static public IP and a verified plugin-owned security group permitting only inbound TCP 22, with stateful outbound access. The group is set during VM creation, so there is no initial period with the permissive default group. Group creation failures abort before disk allocation. SSH allows any source address to support roaming laptops. Existing VM networking is unchanged; shared plugin security groups remain for reuse after VM deletion.

## Uninstall and reinstall

Use `U  Uninstall Nebius plugin` in the N panel, or ask a supported agent to uninstall the Nebius Omarchy plugin. Both invoke `bin/nebius-uninstall`. The panel opens a normal tiled terminal and defaults to **Cancel**. The agent first calls `plan_plugin_uninstall`, explains the removal scope and retention choices, then calls the destructive `uninstall_plugin` tool only after explicit approval. Removing an agent MCP registration alone is not plugin removal. Cloud resources remain outside the uninstall boundary: VMs, disks, projects, networks, and other resources are left exactly as they are and may continue to incur charges.

**These paths work on current Omarchy without any upstream cleanup hook or system patch.** You can also run the shared uninstaller directly in a terminal:

```bash
~/.config/omarchy/plugins/nebius/bin/nebius-uninstall
```

An agent without the Nebius MCP connection should run this script's `--check` first, explain the scope and ask for approval and retention choices. After approval, `--yes --keep-cli --keep-ssh-key --keep-uv` removes the plugin while keeping those three dependencies. Use the corresponding `--remove-cli` or `--remove-ssh-key` only when requested. No agent integration is required to run it.

Confirmed uninstall removes the Omarchy widget/plugin, local state and private MCP cache, the dedicated `omarchy-nebius-mcp` profile and browser token, exact plugin-owned Codex and Claude Code MCP registrations, the plugin-managed `~/.local/bin/nebius` link, and the marked shortcut block. Separate choices ask whether to keep the underlying Nebius CLI, dedicated SSH key and shared uv package; all default to **Keep**, including non-interactive `--yes`. Explicit `--remove-cli`, `--remove-ssh-key` and `--remove-uv` opt into those removals. Keeping the CLI makes reinstall faster; setup restores the terminal command on reinstall. Keeping the key preserves access to existing VMs that trust it—a replacement key will not unlock those machines. uv may now be used by other applications; removing it requires a visible terminal and proof that setup installed it. A CLI is removable only when setup proves ownership and its checksum still matches; a changed binary is preserved. Pre-existing commands, profiles, unrelated MCP servers, keys, shortcuts and packages are untouched.

Uninstall also stops and disables `nebius-ports.service`, removes its unit and saved forwards, and closes its SSH children.

Preflight resolves the Omarchy runtime for agent/SSH shells and checks shell IPC before removing local setup. Active operations block removal. The widget is unloaded before credentials and services are removed; files and cleanup records are retained when a required cleanup step fails. The direct uninstaller verifies that the plugin is absent from disk and Omarchy's registry before reporting success. `--check` probes prerequisites without removing anything; `--dry-run` only describes scope. Failure can leave earlier cleanup stages complete, so retry the uninstaller instead of assuming rollback.

### If the plugin folder was already removed

On Omarchy 4.0.2, bare `omarchy plugin remove nebius` removes only the shell bundle. Manually deleting the folder or removing an MCP registration can also leave services, credentials and local state behind. The plugin cannot intercept those operations.

To finish cleanup, use `bin/nebius-uninstall` from a trusted checkout of this repository on your Omarchy machine. No setup, browser sign-in or cloud provisioning is needed. If you don't have a checkout, clone it into a new directory, review it, then run the script:

```bash
git clone https://github.com/antongisli/omarchy-nebius nebius-uninstall-recovery
./nebius-uninstall-recovery/bin/nebius-uninstall
```

The script checks for an installed bundle, unloads any registered widget, cleans local setup and rescans Omarchy. It skips native bundle removal when that folder is already absent, so recovery can be retried. Only the normal installed plugin path is removed; this recovery checkout and any older manual backups are left for you to delete after checking the result. If shell IPC is unavailable, run from your logged-in Omarchy session. An unavailable CLI needed to remove a dedicated profile or agent registration must be restored first; an expired cloud login does not prevent local cleanup.

### Optional native removal support

The repository declares `entryPoints.uninstall` as `bin/nebius-cleanup`. On a host implementing the proposed cleanup-hook contract, `omarchy plugin remove nebius` invokes the same cleanup before removing plugin files. Cleanup-only mode never recursively removes the plugin; cancellation and failures return nonzero so the host retains the files for retry. Direct panel/agent removal uses the host's explicit cleanup bypass after doing that cleanup itself.

The proposal is [Omarchy PR #11470](https://github.com/omacom/omarchy/pull/11470). It is an optional convenience, not a dependency or a prerequisite for releasing the standalone uninstall flow. Until a host implements it, use the panel, agent tool or direct command above. Do not claim that bare native removal is complete on older hosts. `--skip-cleanup` on hook-enabled hosts deliberately bypasses cleanup and may leave setup artifacts.

After a complete removal, reinstall with the command in the [README](../README.md#install). To exercise every dependency step, explicitly choose to remove the CLI, SSH key and uv; if kept, setup recognizes and reuses them. Start a new Codex or Claude Code session after reinstalling so it loads the new MCP registration.
