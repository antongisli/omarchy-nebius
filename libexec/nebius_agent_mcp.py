#!/usr/bin/env python3
"""Typed, constrained MCP server for the Omarchy Nebius plugin."""

from __future__ import annotations

import json
import sys
import nebius_jobs as jobs
import nebius_ports as ports
import nebius_uninstall as removal
from nebius_catalog import list_images
from typing import Any, Callable

from nebius_core import (
    NebiusError,
    installation_guard,
    connect_vm,
    create_nebius_project,
    create_gpu_vm,
    delete_vm,
    delete_saved_disk,
    vm_storage,
    gpu_capacity,
    list_vms,
    plan_gpu_vm,
    preflight_vm,
    recover_launch,
    repair_rejected_launches,
    start_vm,
    stop_vm,
)


from nebius_runtime import VERSION

SERVER_INFO = {"name": "omarchy-nebius", "version": VERSION}
INSTRUCTIONS = (
    "Manage Nebius GPU VMs. Start with view_gpu_capacity without refresh so choices appear immediately; "
    "state the snapshot age, and request a live refresh only when the user asks. Let the user choose a GPU type. "
    "Group equivalent GPU variants under their gpu_label, preserving machine sizes and exact offering IDs. "
    "Use list_images to offer available custom or public images after project selection. "
    "Only personal VM destination projects are returned; shared tenant projects are intentionally hidden. If the chosen region has no "
    "personal project, offer to create one and explain that it remains even if VM creation is canceled. Treat project "
    "as a secondary placement choice. Let the user edit a proposed name and choose on-demand or preemptible allocation. "
    "Run check_vm_quota before creating a project. A plan with preflight.ready=false cannot be created. "
    "list_vms includes existing VMs in personal projects; connecting may need a username and the user's SSH keys. "
    "If list_vms returns recovery entries, explain the uncertain launch and offer recover_launch before any retry. "
    "For a known local JSON or explicit preemptible admission rejection, repair_rejected_launches can verify and clear the false block without cloud mutations. "
    "Preflight rechecks live platform eligibility, preset, subnet, image, names and reported quotas before disk allocation. It is not a capacity reservation. "
    "A plan may include reusable_disk: tell the user that this existing boot disk will be reused and no new disk allocated. "
    "Never call create_gpu_vm until the user "
    "has seen the plan, cost warning and allocation type and has answered a plain-language confirmation. "
    "There is no auto-stop feature: VMs run until stopped manually. Explain this before creation. "
    "The client approval for this write tool is the final confirmation—never ask the user to type a magic phrase. "
    "Never delete without plain-language confirmation and the destructive tool approval. Remind the user that stopped "
    "disks remain billable."
    " Creation and lifecycle tools return background job IDs; use list_operations to follow completion. "
    "New VMs have static public IPv4 with SSH-only ingress. Use forward_port for local application access; ask which ports."
    " To uninstall this Omarchy plugin, call plan_plugin_uninstall, explain the cloud-resource warning and ask about "
    "keeping the CLI, dedicated SSH key and shared uv runtime. After explicit approval, call uninstall_plugin. "
    "This works on current Omarchy without a host cleanup hook. Do not use bare omarchy plugin remove: "
    "on older hosts it removes only the bundle. Removing only an agent MCP registration or deleting the plugin folder is not a complete uninstall. "
    "Never report removal as complete without the uninstaller's verified result."
)


def tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    read_only: bool,
    destructive: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": read_only,
            "openWorldHint": True,
        },
    }


TOOLS = [
    tool("list_operations", "List background operations, their progress, errors and results.", {}, [], read_only=True),
    tool("list_ports", "List saved localhost port forwards and connection status.", {}, [], read_only=True),
    tool("forward_port", "Save a loopback-only SSH forward that reconnects after login. Ask which application port to forward.",
         {"vm_id": {"type": "string"}, "remote_port": {"type": "integer", "minimum": 1, "maximum": 65535},
          "local_port": {"type": "integer", "minimum": 1024, "maximum": 65535}, "username": {"type": "string"}},
         ["vm_id", "remote_port"], read_only=False),
    tool("manage_port", "Pause, resume or remove a saved local port forward.",
         {"id": {"type": "string"}, "action": {"type": "string", "enum": ["pause", "resume", "remove"]}},
         ["id", "action"], read_only=False),
    tool(
        "view_gpu_capacity",
        "List cached GPU types and on-demand/preemptible availability immediately, with snapshot age and refresh status.",
        {"refresh": {"type": "boolean", "description": "Try a live cloud refresh; falls back to cached capacity if Nebius times out."}},
        [],
        read_only=True,
    ),
    tool(
        "list_vms",
        "List VMs across personal projects with state, region, GPU, allocation and SSH details. Set refresh to retrieve current state.",
        {"refresh": {"type": "boolean"}},
        [],
        read_only=True,
    ),
    tool("list_images", "List available public and custom images for chosen machine configurations and a project.",
         {"offering_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
          "project_id": {"type": "string"}}, ["offering_ids", "project_id"], read_only=True),
    tool(
        "plan_gpu_vm",
        "Prepare a GPU VM plan with preflight checks, chosen allocation and editable name. No automatic stop is scheduled. Does not create cloud resources.",
        {
            "name": {"type": "string", "description": "Optional DNS-safe VM name."},
            "image_id": {"type": "string", "description": "Exact image ID from list_images; omit for the default Ubuntu/CUDA family."},
            "disk_gib": {"type": "integer", "minimum": 50, "maximum": 30720},
            "offering_id": {"type": "string", "description": "GPU option returned by view_gpu_capacity."},
            "project_id": {"type": "string", "description": "Optional compatible project returned with the GPU option."},
            "allocation": {"type": "string", "enum": ["on_demand", "preemptible"], "default": "on_demand"},
        },
        ["offering_id"],
        read_only=True,
    ),
    tool(
        "create_project",
        "Create a named personal project with its default network in the chosen GPU region. Check quota first; confirm the name and region with the user.",
        {
            "region": {"type": "string", "description": "Region from the chosen GPU offering."},
            "name": {"type": "string", "description": "Project name chosen by the user; propose gpu-<region>."},
        },
        ["region", "name"],
        read_only=False,
    ),
    tool(
        "create_gpu_vm",
        "Create the previously reviewed billable VM. The agent's write-tool approval is the final user confirmation.",
        {"plan_id": {"type": "string"}},
        ["plan_id"],
        read_only=False,
    ),
    tool(
        "start_vm",
        "Start a stopped VM in a personal project and wait for its address. Resumes compute billing.",
        {"vm_id": {"type": "string"}},
        ["vm_id"],
        read_only=False,
    ),
    tool(
        "stop_vm",
        "Stop a VM in a personal project. Its disk remains billable.",
        {"vm_id": {"type": "string"}},
        ["vm_id"],
        read_only=False,
    ),
    tool(
        "delete_vm",
        "Permanently delete a plugin-created VM and its boot disk after a plain-language confirmation and destructive tool approval.",
        {
            "vm_id": {"type": "string"},
            "confirmed": {"type": "boolean", "description": "True only after the user plainly confirmed deletion."},
        },
        ["vm_id", "confirmed"],
        read_only=False,
        destructive=True,
    ),
    tool(
        "connect_vm",
        "Open a terminal and SSH into a running VM in a personal project. Existing VMs use configured SSH keys or agent.",
        {"vm_id": {"type": "string"}, "username": {"type": "string", "description": "SSH login user if not known."}},
        ["vm_id"],
        read_only=True,
    ),
]
TOOLS.append(tool(
    "check_vm_quota", "Read-only quota check; include project, platform and preset to check project-specific GPU eligibility too.",
    {"region": {"type": "string"}, "project_id": {"type": "string"},
     "platform": {"type": "string"}, "preset": {"type": "string"}, "gpu_count": {"type": "integer", "minimum": 1},
     "allocation": {"type": "string", "enum": ["on_demand", "preemptible"]}},
    ["region"], read_only=True,
))
TOOLS.append(tool(
    "recover_launch", "Reconcile an interrupted launch by its saved request ID. Restore management and the plugin SSH key. Does not schedule stops, create or delete cloud resources.",
    {"request_id": {"type": "string"}}, ["request_id"], read_only=False,
))
TOOLS.append(tool(
    "repair_rejected_launches", "Verify narrowly recognized rejected requests, clear only their false recovery blocks and preserve their boot disks for reuse or confirmed cleanup. Timeouts remain uncertain. No cloud resources are created or deleted.",
    {}, [], read_only=False,
))
TOOLS.extend([
    tool("plan_plugin_uninstall", "Check local uninstall prerequisites and explain removal scope, retained cloud resources and optional tools. No cloud access or deletion.",
         {}, [], read_only=True),
    tool("uninstall_plugin", "Uninstall the Nebius Omarchy plugin and its local setup using the panel's verified uninstaller. Confirm scope and retention choices first. Never deletes cloud resources.",
         {"confirmed": {"type": "boolean"}, "keep_cli": {"type": "boolean"},
          "keep_ssh_key": {"type": "boolean"}, "keep_uv": {"type": "boolean"}},
         ["confirmed", "keep_cli", "keep_ssh_key", "keep_uv"], read_only=False, destructive=True),
    tool("delete_saved_disk", "Permanently delete a verified unused plugin boot disk after explicit confirmation. Refuses attached or protected disks.",
         {"disk_id": {"type": "string"}, "confirmed": {"type": "boolean"}}, ["disk_id", "confirmed"], read_only=False, destructive=True),
    tool("inspect_vm_storage", "List a VM's attached disk names, IDs, sizes and cleanup boundaries.",
         {"vm_id": {"type": "string"}}, ["vm_id"], read_only=True),
])


def _call(name: str, arguments: dict[str, Any]) -> Any:
    handlers: dict[str, Callable[[], Any]] = {
        "plan_plugin_uninstall": removal.plan,
        "uninstall_plugin": lambda: removal.uninstall(confirmed=arguments.get("confirmed"),
            keep_cli=arguments.get("keep_cli"), keep_ssh_key=arguments.get("keep_ssh_key"), keep_uv=arguments.get("keep_uv")),
        "view_gpu_capacity": lambda: gpu_capacity(force_refresh=arguments.get("refresh") is True),
        "list_images": lambda: list_images(arguments["offering_ids"], arguments["project_id"]),
        "list_vms": lambda: list_vms(force_refresh=arguments.get("refresh") is True),
        "recover_launch": lambda: recover_launch(str(arguments.get("request_id", ""))),
        "repair_rejected_launches": lambda: repair_rejected_launches(),
        "plan_gpu_vm": lambda: plan_gpu_vm(
            arguments.get("name"), arguments.get("offering_id"), arguments.get("project_id"),
            arguments.get("allocation", "on_demand"), arguments.get("auto_stop_hours", 0),
            arguments.get("image_id", ""), arguments.get("disk_gib")
        ),
        "create_project": lambda: jobs.submit(["create-project", "--region", str(arguments.get("region", "")),
                                               "--name", str(arguments.get("name", "")), "--confirmed"]),
        "create_gpu_vm": lambda: jobs.submit(["create", "--plan-id", str(arguments.get("plan_id", ""))]),
        "start_vm": lambda: jobs.submit(["start", "--vm-id", str(arguments.get("vm_id", ""))]),
        "stop_vm": lambda: jobs.submit(["stop", "--vm-id", str(arguments.get("vm_id", ""))]),
        "delete_vm": lambda: submit_delete("delete", "--vm-id", arguments.get("vm_id"), arguments.get("confirmed")),
        "delete_saved_disk": lambda: submit_delete("delete-disk", "--disk-id", arguments.get("disk_id"), arguments.get("confirmed")),
        "list_operations": lambda: {"jobs": jobs.jobs()},
        "list_ports": lambda: {"ports": ports.listing()},
        "forward_port": lambda: ports.add(arguments["vm_id"], arguments["remote_port"], arguments.get("local_port"), arguments.get("username")),
        "manage_port": lambda: ports.change(arguments["id"], arguments["action"]),
        "inspect_vm_storage": lambda: vm_storage(str(arguments.get("vm_id", ""))),
        "connect_vm": lambda: connect_vm(str(arguments.get("vm_id", "")), username=arguments.get("username")),
        "check_vm_quota": lambda: preflight_vm(str(arguments.get("region", "")),
                                                arguments.get("allocation", "on_demand"), arguments.get("platform", ""),
                                                arguments.get("gpu_count", 1), project_id=arguments.get("project_id"), preset=arguments.get("preset", "")),
    }
    if name not in handlers:
        raise NebiusError(f"Unknown tool: {name}")
    if name in {"plan_plugin_uninstall", "uninstall_plugin"}:
        return handlers[name]()
    with installation_guard():
        return handlers[name]()


def submit_delete(command, flag, resource_id, confirmed):
    if confirmed is not True:
        raise NebiusError("Deletion requires an explicit confirmation")
    return jobs.submit([command, flag, str(resource_id or ""), "--confirmed"])


def _write(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _result(request_id: Any, value: Any) -> None:
    serialized = json.dumps(value, indent=2, sort_keys=True)
    _write(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": serialized}],
                "structuredContent": value,
                "isError": False,
            },
        }
    )


def _error(request_id: Any, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return
    if method == "initialize":
        _write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": INSTRUCTIONS,
                },
            }
        )
        return
    if method == "ping":
        _write({"jsonrpc": "2.0", "id": request_id, "result": {}})
        return
    if method == "tools/list":
        _write({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        return
    if method == "tools/call":
        params = message.get("params") or {}
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            _error(request_id, -32602, "Tool arguments must be an object")
            return
        try:
            _result(request_id, _call(str(params.get("name") or ""), arguments))
        except (NebiusError, removal.UninstallError) as error:
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": str(error)}],
                        "isError": True,
                    },
                }
            )
        return
    _error(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                handle(value)
        except json.JSONDecodeError:
            continue
        except Exception as error:  # Keep the stdio server alive for the next request.
            print(f"omarchy-nebius MCP: {error}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
