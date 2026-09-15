# Release notes

## Unreleased

- Managed Kubernetes worker nodes are hidden from VM listings and the running-VM
  badge by default. **E · Settings** can include them as clearly identified,
  cluster-managed resources without direct VM lifecycle actions.
- SSH sessions now open with the configured terminal's native application
  identity, so Omarchy's universal Super+C and Super+V shortcuts keep working
  after connecting to a VM.
- Deleting a VM now forgets all of its saved port forwards, clears their local
  status and logs, and closes their supervised SSH tunnels without affecting
  mappings for other VMs.

## 0.8.2 · 2026-09-13 · Terminal launcher fix

- Fixed `nebius` resolving its runtime relative to the `~/.local/bin` symlink
  instead of the installed plugin directory.
- Added a regression that invokes the launcher through the real terminal link.

## 0.8.1 · 2026-09-13 · Nebius CLI in regular terminals

- Setup now makes the verified CLI available as `nebius` through a managed
  `~/.local/bin` link, without editing shell profiles.
- Existing terminal commands and paths are preserved. Full uninstall removes
  only the exact plugin-created link; keeping the underlying CLI still makes
  reinstall faster.

## 0.8.0 · 2026-09-12 · Public beta · Images and VM lifecycle

- Added a short account and billing signup guide; clarified image defaults,
  keyboard shortcuts and ownership checks for VM deletion.
- Refreshed documentation captures and fixed blank embedded panels in Qt's
  offscreen cover renderer, with a regression test for preserved terminal cells.
- Fixed new-VM networking: use `eth0` instead of the reserved interface name
  `default`, which caused guest IPv4 setup and first SSH to fail.

- The timing stage is named SSH ready. Initial probes retry after 0.5 seconds
  with a 2-second connection timeout, returning to patient retries after 30 seconds.
  Saved details include connection preparation and individual probe attempts.

- VM lists update operation states immediately and refresh cloud inventory in the
  background. Completed deletions disappear without pressing refresh; navigation
  and search remain in place, and failed reads retain the last known inventory.

- Launch/start now wait for verified SSH login. Stage durations and total time to
  SSH ready remain on a saved result screen, including after connection attempts.
  Activity and VM actions reopen the report; failed probes never open a session.

- Deletion verifies your Nebius identity against VM creation audit history instead
  of requiring a local plugin record. Existing VMs remain deletable after reinstall;
  confirmation identifies the exact boot disk and secondary disks are kept.

- VM settings can select existing public and accessible custom images for the
  chosen region and machine, with compatibility details and editable disk size.
- Image access, region, hardware restrictions, readiness and disk fit are checked
  before allocation. Agents can list images and plan with an exact image ID.
- RTX PRO 6000 and L40S platform variants share product names and equivalent
  configurations; placement retains the exact underlying platform and preset.

## 0.7.10 · 2026-09-12 · Lifecycle response handling

- Start, stop and deletion now accept the CLI's plain-text asynchronous operation ID, as well as JSON-encoded IDs. Resource and operation-status responses still require JSON.
- Operation IDs are validated and saved before polling. Invalid replies retain the submission journal, so retrying cannot silently submit the same action twice.
- Added stdout-level regressions for instance and disk operations, failed polling, invalid responses and read-only reconciliation of an uncertain request.


## 0.7.9 · 2026-09-12 · SSH after reinstall

- SSH and port forwarding offer the retained Nebius key for VMs labelled as created by this plugin, even after uninstall has removed the local VM registry.
- SSH identity is separate from management ownership: reconnecting does not adopt a VM or unlock deletion. Unrelated VMs retain their normal SSH configuration and agent.
- Existing saved forwards recover the identity from live VM metadata. No keys are regenerated and no cloud resources are changed.

## 0.7.8 · 2026-09-12 · Configurable keyboard shortcut

- A dedicated Super+Ctrl+M shortcut opens Nebius independently of bar position. Setup only installs it when free and respects a saved or disabled choice.
- Shift+K opens shortcut settings from the panel or manager, including before account setup. Tab/arrows select fields and modifiers; type a key, Enter saves, Escape cancels.
- Existing bindings are preserved. Conflicts block saving; syntax, compositor reload and live-binding verification protect changes. Uninstall verifies removal of the managed shortcut too.
- Panel labels use Shift+J and Shift+K so lowercase j/k remain navigation keys.

## 0.7.7 · 2026-09-12 · Standalone uninstall

- Panel and agent removal share one local-only uninstaller, with preflight, widget unload and post-removal checks.
- Added typed agent uninstall planning and confirmation tools; removing only an MCP registration is no longer described as uninstalling the Omarchy plugin.
- Non-interactive removal keeps the CLI, SSH key and shared uv package by default. Removing uv requires a visible terminal; unrelated installations are preserved.
- Failed cleanup returns an error instead of success. Cleanup-only cancellation aborts the host's removal; active mutations block uninstall.
- Panel, direct-command and agent removal work on unmodified Omarchy without cleanup hooks; CI tests that path against pinned upstream code. The optional `entryPoints.uninstall` adapter supports the proposed upstream hook but is not a release dependency.
- Running the uninstaller from a checkout can finish local cleanup when a generic remove command or an agent already deleted the installed bundle. Recovery is retryable; stale shell registration is reported rather than hidden.
- Added explicit standalone removal instructions for users and agents. Bare `omarchy plugin remove nebius` on older hosts still removes only the bundle; use the full uninstaller. End-to-end removal/reinstall remains a release check, independent of upstream PR acceptance.

## 0.7.6 · 2026-09-12 · Public beta

Includes the 0.7.4 and 0.7.5 work.

### GPU workflow and SSH

- First SSH waits for a real login, with progress, cancellation and retained failure details.
- Direct keyboard actions across VM menus; one review for start, stop and deletion.
- Escape or B leaves submitted work running in the background.
- Independent VM lifecycle jobs run concurrently and preserve their own results.
- New VMs use static public IPv4 and an SSH-only security group; existing VMs are unchanged.

### Remote applications

- Saved loopback-only SSH forwards reconnect through a systemd user service.
- Both ports share one keyboard form, with Tab/arrows and a route diagram.
- Ports and Activity refresh every two seconds while retaining selection and search.
- Historical Activity entries show completed results or actionable errors.
- Connected means the SSH tunnel is ready, not that the remote application is healthy.

### Setup and release quality

- Existing Nebius CLI versions are never overwritten. A matching version is reused; otherwise a verified, versioned private copy is installed.
- Uninstall checks the exact owned CLI path and checksum; borrowed or modified binaries are preserved.
- Uninstall removes the plugin-private MCP cache without touching unrelated caches.
- MCP version metadata follows the plugin manifest.
- Updated README, upgrade instructions and synthetic-data screenshots from production UI code.
- Expanded installer, keyboard, ports, Activity and SSH regression coverage.

### Upgrade

Run `omarchy plugin update nebius`. Close and reopen existing Nebius terminals and start new agent sessions. Account, SSH key and saved mappings are retained. Use S to repair or reconnect if prompted. The versioned widget path avoids requiring a desktop restart.

### Limits

No application templates, file synchronization or auto-stop. Applications must be started on the VM separately. Cloud resources remain billable as applicable after plugin removal. This is a public beta, not marketplace approval; fresh billable end-to-end provisioning requires an explicitly confirmed smoke test.
