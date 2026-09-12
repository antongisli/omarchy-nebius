# Release notes

## 0.7.6 · 2026-09-12 · Public beta

Includes the previously unpublished 0.7.4 and 0.7.5 work.

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
