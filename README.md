<p align="center">
  <img src="assets/nebius-logo.svg" alt="Nebius" width="175">
</p>

<h1 align="center">Need a bigger GPU?</h1>

<p align="center">
  <strong>Find capacity. Launch a VM. Jump in.</strong><br>
  Nebius cloud GPUs, a few keystrokes from your Omarchy desktop.
</p>

<p align="center">
  <a href="#install"><img src="https://img.shields.io/badge/Omarchy-Quattro-052B42?style=flat-square" alt="Omarchy Quattro"></a>
  <a href="#use-your-agent"><img src="https://img.shields.io/badge/agents-Codex%20%2B%20Claude%20Code-E0FF4F?style=flat-square&labelColor=052B42" alt="Codex and Claude Code"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/code-MIT-052B42?style=flat-square" alt="MIT licensed code"></a>
  <a href="https://github.com/antongisli/omarchy-nebius/actions/workflows/check.yml"><img src="https://github.com/antongisli/omarchy-nebius/actions/workflows/check.yml/badge.svg" alt="Checks"></a>
</p>

![Nebius GPU for Omarchy: GPU choices and local SSH port forwarding](preview.png)

*Preview rendered from the plugin interface with example data. GPU availability is checked in your account.*

Out of VRAM in **ComfyUI**? Want to try a larger model with **vLLM**, or connect **Open WebUI** to your own model server? Your desktop doesn't have to be the limit.

**Nebius GPU** brings cloud GPU discovery and VM management into Omarchy. Choose a GPU and region, review the cost and configuration, then open an SSH session. Keep your keyboard workflow while running the workload on a bigger machine.

The plugin gets you a CUDA-ready Ubuntu VM. You install your application and models on it; app templates and automatic workload migration are not included in this release.

## From “out of memory” to a machine you can use

| You want to… | The plugin helps you… |
| --- | --- |
| Run a workflow that no longer fits locally | Compare GPU families and regional availability before choosing a VM. |
| Try a new model or benchmark vLLM | Launch a CUDA-ready machine with an editable name and a dedicated SSH key. |
| Keep experimenting without losing your place | Jump back into running VMs; see state and ongoing operations from Omarchy. |
| Choose cost versus interruption risk | Switch between on-demand and preemptible, with price estimates and configuration review. |
| Keep cloud housekeeping manageable | Start or stop VMs, inspect storage, and explicitly delete plugin-managed VMs and boot disks. |

GPU choice comes first. Project placement comes afterward, with your personal projects preferred and an editable project proposal when you need one in another region.

## Install

Requires **Omarchy Quattro with plugin support**, a Nebius account, and access to billable compute. GPU availability and your account's quota determine what you can launch.

```bash
omarchy plugin add https://github.com/antongisli/omarchy-nebius --enable
```

Open the lime Nebius icon in your bar and choose **Set up Nebius**. A normal tiled terminal guides you through dependency installation, browser sign-in, project discovery and SSH setup. It checks installed **Codex** and **Claude Code** agents and adds Nebius tools to both when present.

An agent is optional for the keyboard interface. For natural-language requests, install and sign in to either supported agent through Omarchy, then run setup again.

New to Nebius? [Create an account](https://console.nebius.com/) · [Check GPU pricing](https://nebius.com/prices)

<details>
<summary>What gets installed?</summary>

- **uv**, from Arch's official repository through `omarchy-pkg-add uv`. The visible package installer may ask for your password.
- **Nebius CLI 0.12.269**, verified by SHA-256. A matching existing CLI is reused without claiming ownership. Otherwise a private copy is installed at `~/.local/share/nebius/cli/0.12.269/nebius` (respecting `XDG_DATA_HOME`); your existing CLI is never overwritten.
- **Official Nebius MCP**, pinned to commit `6388bf779acdd331d9b2016230b37f8bf7177e12` and cached privately. Its runtime requires Python 3.13+; uv provisions the environment.
- A dedicated **browser-auth profile** and **SSH key** for this plugin.
- A constrained **`nebius` MCP registration** in each installed supported agent. Conflicting registrations are never overwritten.

Omarchy supplies the ordinary terminal tools used by setup, including Python, Bash, jq, curl, OpenSSH, fzf, gum and util-linux. No shell profile is edited. [Full setup and security details](docs/reference.md).

</details>

## Update

To update, run `omarchy plugin update nebius`, then close and reopen any existing Nebius terminal. The widget reloads through its versioned entry point; no desktop restart is needed. Start new agent sessions to load updated tools. Run **S · Set up / reconnect** if prompted; your account, SSH key and saved forwards are reused. [Release notes](CHANGELOG.md).

## Stay on the keyboard

| Key in the Nebius panel | Action |
| --- | --- |
| **G** | Get a GPU: choose → review → create |
| **J** | Jump into a running VM |
| **V** | Your VMs: overview and lifecycle actions |
| **C** | GPU capacity by family and region |
| **P** | Saved local ports: add, open, pause or remove a forward |
| **A** | Activity: concurrent operations, progress and results |
| **S** | Set up or reconnect your account |
| **U** | Uninstall Nebius plugin |

Inside the terminal, use **arrows** or **j/k**, **Enter**, **Esc**, **/** to search, and **?** for help. GPU menus show an **On-demand / Preemptible switch at the top**: **P** toggles the highlighted option and updates the displayed availability; **R** fetches a fresh capacity snapshot. The terminal manager uses **Shift+J** for Jump because lowercase **j** moves down.

Before setup, the bar shows only the Nebius icon. After setup, a small **running VM count badge** appears on the icon: **0–9**, then **9+**, with the exact count in the tooltip. An unknown or stale count displays **?**, so failed refreshes never look like an empty account. Middle-click the icon to jump into a VM.

For direct desktop shortcuts, add the optional [Super+Ctrl+G/J/M bindings](config/keybindings.lua) after checking for conflicts with your existing bindings.

## Use remote applications locally

Choose **P · SSH port forwarding → N · Add port**, select the VM, and enter the application's remote TCP port. The local port defaults to the same number (or an unprivileged alternative for ports below 1024). If it is occupied, choose another. For example, remote port 8188 becomes `http://127.0.0.1:8188` on your laptop. From **Your VMs**, highlight a VM and press **P** to manage its ports directly.

Both ports share one form: **Tab / ↑↓** switches fields, **←→** moves the cursor, **Enter** saves, and **Esc** cancels. A live diagram shows `this computer → SSH → VM application`. The local port follows the remote port until you customize it; validation keeps both values on screen.

Forwards listen only on the laptop's loopback address and travel through encrypted SSH. No public application port, domain or HTTPS certificate is needed. Start the application on the VM, listening on its loopback interface; Docker applications must publish their port to the VM loopback interface too.

Enabled mappings are saved and restored by a systemd user service after login, independently of the terminal window. They reconnect after sleep or network changes. Stopping a VM preserves its mappings without starting the VM automatically; deleting it disables them. The Ports list updates every two seconds without resetting selection or search; **R** refreshes immediately. It offers actions to open HTTP apps in a browser, copy the address, inspect errors, pause/resume or remove a mapping. **Connected** describes the SSH tunnel, not application health. Requests in flight can fail during sleep or reconnect; refresh the browser afterward.

<details>
<summary>See port forwarding, VM management and Activity</summary>

![One form for both ports, with keyboard navigation and an SSH route diagram](docs/screenshots/port-form.png)

![Saved port forwards with live tunnel status](docs/screenshots/ports.png)

![VM overview with direct SSH, start, stop and deletion shortcuts](docs/screenshots/overview.png)

![Activity separates in-progress work from saved results and actionable failures](docs/screenshots/activity.png)

These captures use the production drawing code with synthetic example data, not a user's account. They do not imply that ComfyUI or model-serving applications are installed automatically.

</details>

New VMs receive a **static public IPv4 address and an explicit security group allowing only inbound SSH (TCP 22)**. Outbound traffic is allowed for package and model downloads. Existing VMs are not modified. The SSH rule permits any source IP so changing laptop networks does not lock you out; SSH key authentication is still required. Plugin-owned SSH security groups can be reused within a network and remain after VM deletion.

Start, stop and deletion return immediately to the VM overview. Independent VMs can run operations concurrently; conflicting operations on the same VM are rejected. Activity stores each job separately. If a lifecycle worker is interrupted, **Activity → operation → Resume operation** reconciles its saved cloud operation before finishing cleanup. VM deletion always finishes before checking and deleting its boot disk.

Activity puts running operations first, with recent results below, and updates every two seconds while preserving selection and search. Older completed jobs use their saved results or history, not a misleading waiting message. Interrupted work says **Check outcome**; Resume is offered only when the exact request was saved.

From the VM list: **C** connects, **P** opens SSH ports, **S** stops, **T** starts, and **D** reviews deletion for the highlighted VM. Start/stop/delete each use one review, with a named confirmation key and **Esc** to cancel. Deletion never submits just because you press Enter to read the next page. Action menus show letter keys beside choices and in the footer; resource lists offer number keys and `/` search. During a submitted operation, **Esc or B** returns to the overview without cancelling the work; **A** reopens Activity.

SSH checks login readiness for up to two minutes before opening the session, including the first boot's user/key setup. The wait shows progress and **Esc** returns without stopping the VM. A failed connection stays on screen with retry and username actions; the latest diagnostic is retained in local `ssh-last-error.json`. Changed host keys are never accepted automatically.

## Use your agent

Setup detects your Omarchy default agent, checks sign-in, and registers the same constrained tools with installed **Codex and Claude Code**. Start a new agent session after setup, then try:

> Show me GPU availability by region.

> I need a GPU for a vLLM experiment. Show me options and the estimated cost before creating anything.

> List my running VMs and help me connect to one.

The tools cover capacity, project placement, planning, VM creation and lifecycle, and SSH. Billable and destructive operations require approval. The agent's own subscription or API billing is separate from Nebius compute billing.

## Know what is happening

- **Preflight before allocation.** Check capacity advice, GPU and disk quota, image, subnet, allocation eligibility and request syntax before a VM launch allocates a disk.
- **Review before billing.** See the machine, project, allocation, boot disk and available price estimate before confirmation.
- **Progress that survives the window.** A submitted launch continues in the background. Reopen activity to see its result or error.
- **Failures stay visible.** Uncertain launches have recovery actions. Retained boot disks stay available for reuse or explicit cleanup.
- **Reconnect when needed.** Expired Nebius sessions prompt browser sign-in; there is no service-account setup.

Preflight is not a capacity reservation or a guarantee of permissions. Stop VMs manually when finished: **auto-stop is not included**. Stopped VMs retain billable storage until the disks are deleted.

## Remove or reinstall

Choose **U · Uninstall Nebius plugin** for the full local cleanup. You can keep the CLI for faster reinstall and keep the SSH key to preserve access to existing VMs.

**Cloud resources remain unchanged and may keep costing money.** Removing the plugin does not stop or delete VMs, disks, projects or networks. Direct `omarchy plugin remove nebius` removes the widget bundle only; use the plugin's uninstall action to also remove its local setup.

## Project status

Public beta for Omarchy Quattro. Built and checked on a real Omarchy desktop, with regression coverage for keyboard navigation, narrow terminals, preflight failures, installer ownership and resource cleanup. [Release notes](CHANGELOG.md) · [Development guide](docs/development.md) · [Known limits and implementation details](docs/reference.md) · [Report an issue](https://github.com/antongisli/omarchy-nebius/issues)


Maintained by [Anton Smith](https://github.com/antongisli). Uses the official Nebius CLI and MCP and has no dependency on the separate `other-tool` integration. Code is [MIT licensed](LICENSE); Nebius marks remain the property of their owners. [Brand asset sources](assets/NOTICE.md).
