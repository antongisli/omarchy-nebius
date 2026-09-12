#!/usr/bin/env python3
"""Constrained Nebius operations used by the panel and supported agent MCP integrations."""

from __future__ import annotations

import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import hashlib
import ipaddress
import fcntl
import functools
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import threading
from typing import Any

from nebius_runtime import cli_path


PROFILE = "omarchy-nebius-mcp"
MANAGED_BY = "omarchy-nebius"
HOME = Path.home()
CLI = cli_path()
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "nebius"
PLAN_DIR = STATE_DIR / "plans"
REGISTRY_FILE = STATE_DIR / "vms.json"
OPERATION_FILE = STATE_DIR / "operation.json"
CAPACITY_FILE = STATE_DIR / "capacity.json"
PROJECTS_FILE = STATE_DIR / "projects.json"
INVENTORY_FILE = STATE_DIR / "inventory.json"
CONNECTIONS_FILE = STATE_DIR / "connections.json"
CREDENTIALS_FILE = HOME / ".nebius/credentials.yaml"
SSH_KEY = HOME / ".ssh/nebius-ed25519"
SSH_USER = "dev"
DEFAULT_DISK_GIB = 200
IMAGE_FAMILY = "ubuntu24.04-cuda13.0"

# Published PAYG USD prices checked against the official Compute pricing page.
# Unified platforms are per GPU-hour. L40S also charges CPU and RAM separately.
PREEMPTIBLE_GPU_USD = {
    "gpu-b300-sxm": 4.30,
    "gpu-b200-sxm": 3.95,
    "gpu-b200-sxm-a": 3.95,
    "gpu-h200-sxm": 2.45,
    "gpu-h100-sxm": 2.15,
    "gpu-rtx6000": 0.95,
    "gpu-rtx6000-a": 0.95,
    "gpu-l40s-a": 0.65,
    "gpu-l40s-d": 0.65,
}
ON_DEMAND_GPU_USD = {
    "gpu-b300-sxm": 7.85, "gpu-b200-sxm": 7.15, "gpu-b200-sxm-a": 7.15,
    "gpu-h200-sxm": 4.50, "gpu-h100-sxm": 3.85,
    "gpu-rtx6000": 1.80, "gpu-rtx6000-a": 1.80,
    "gpu-l40s-a": 1.35, "gpu-l40s-d": 1.35,
}
PRICING_URL = "https://docs.nebius.com/compute/resources/pricing"
PRICING_CHECKED_AT = "2026-09-09"
DISK_USD_PER_GIB_MONTH = 0.071


class NebiusError(RuntimeError):
    pass


def _cli_rejected_json(error: str) -> bool:
    return bool(re.match(r"^(?:Nebius: )?Error: read protojson from positional arguments:", error.strip()))


_mutation_state = threading.local()


@contextlib.contextmanager
def installation_guard():
    """Keep uninstall from discarding state while an operation is using it."""
    if not Path(__file__).exists():
        raise NebiusError("The Nebius plugin was removed. Close this old session.")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "uninstall.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise NebiusError("Nebius is being uninstalled. Wait for removal to finish.") from error
        yield


@contextlib.contextmanager
def mutation_guard(*, wait=False, resource="global"):
    owned = getattr(_mutation_state, "owned", set())
    if resource in owned:
        yield
        return
    name = "mutation.lock" if resource == "global" else "mutation-" + hashlib.sha256(resource.encode()).hexdigest() + ".lock"
    with installation_guard(), (STATE_DIR / name).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise NebiusError("Another operation is running for this resource. Open Activity to follow it.") from error
        _mutation_state.owned = owned | {resource}
        try:
            yield
        finally:
            _mutation_state.owned = owned


def cloud_mutation(function):
    @functools.wraps(function)
    def locked(*args, **kwargs):
        key = "global"
        if function.__name__ in {"start_vm", "stop_vm", "delete_vm"}:
            key = str(args[0] if args else kwargs["vm_id"])
        with mutation_guard(wait=bool(kwargs.get("automatic")), resource=key):
            return function(*args, **kwargs)
    return locked


def explain_error(error: str) -> dict[str, str]:
    """Keep a human-sized explanation separate from the diagnostic payload."""
    lower = error.lower()
    if "preflight failed in " in lower:
        message = error.splitlines()[0].removeprefix("Nebius: ")[:300]
        recovery = "No new VM or disk was created. Adjust the settings or restore access, then run preflight again. Details show the failed check."
    elif "preemptible instances of platform" in lower and "are not allowed in" in lower:
        message = "This GPU platform does not allow preemptible VMs in the selected project."
        recovery = "Choose on-demand or another GPU/project. Check Your VMs for the rejected request's saved boot disk."
    elif _cli_rejected_json(error):
        message = "The CLI rejected the VM request before it was submitted."
        recovery = "No VM was created. A confirmed boot disk is kept for reuse on your next reviewed launch."
    elif "quota" in lower and ("exceeded" in lower or "insufficient" in lower):
        if "network-ssd" in lower or "ssd storage" in lower:
            message = "Not enough SSD storage quota in this region."
        else:
            message = "This region does not have enough quota for the selected VM."
        recovery = "Choose another region, or request a quota increase in the Nebius console."
    elif "expired" in lower or "unauthenticated" in lower:
        message = "Your Nebius session needs reconnecting."
        recovery = "Use Account / reconnect. Your selections and existing resources are kept."
    elif "permissiondenied" in lower or "permission denied" in lower:
        message = "Your account cannot perform this action."
        recovery = "Check access to the selected project in the Nebius console."
    elif "timed out" in lower or "deadlineexceeded" in lower:
        message = "Nebius did not finish responding in time."
        recovery = "Refresh the overview before retrying a create action; the cloud request may still finish."
    elif "alreadyexists" in lower or "already exists" in lower:
        message = "That name is already in use."
        recovery = "Edit the name or choose the existing project."
    else:
        message = error.splitlines()[0].removeprefix("Error: ")[:220]
        recovery = "Go back to edit your choices. Open details for the full response."
    return {"message": message, "recovery": recovery, "details": error}


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _run(command: list[str], *, timeout: int = 90, input_text: str | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NebiusError(f"Could not run {command[0]}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise NebiusError(detail[-2000:])
    return result.stdout


def _profile_credential_expiry() -> int | None:
    try:
        lines = CREDENTIALS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    profile_indent: int | None = None
    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.removesuffix(":").endswith(f"//{PROFILE}"):
            profile_indent = indent
            continue
        if profile_indent is not None:
            if stripped and indent <= profile_indent:
                return None
            match = re.match(r"^\s+expires_at:\s*([0-9]+)\s*$", line)
            if match:
                return int(match.group(1))
    return None


def run_cli(arguments: list[str], *, timeout: int = 90, parse_json: bool = True) -> Any:
    if not CLI.is_file() or not os.access(CLI, os.X_OK):
        raise NebiusError(f"Nebius CLI is missing at {CLI}")
    credential_expiry = _profile_credential_expiry()
    if credential_expiry is not None and credential_expiry <= int(time.time()) + 30:
        raise NebiusError("Nebius account session expired. Open the N widget and reconnect your account.")
    command = [
        str(CLI),
        "--profile",
        PROFILE,
        "--no-browser",
        "--no-progress",
        "--no-check-update",
        "--auth-timeout",
        "20s",
        "--timeout",
        f"{max(10, timeout - 5)}s",
        *arguments,
    ]
    output = _run(command, timeout=timeout)
    if not parse_json:
        return output.strip()
    try:
        return json.loads(output or "{}")
    except json.JSONDecodeError as error:
        raise NebiusError(f"Nebius CLI returned invalid JSON: {error}") from error


def profile_value(field: str) -> str:
    return str(run_cli(["config", "get", field], timeout=30, parse_json=False)).strip()


def project_context(project_id: str | None = None, tenant_id: str | None = None) -> dict[str, str]:
    project_id = project_id or profile_value("parent-id")
    tenant_id = tenant_id or profile_value("tenant-id")
    if not re.fullmatch(r"project-[a-z0-9-]+", project_id):
        raise NebiusError("The dedicated Nebius profile has no selected project")
    if not re.fullmatch(r"tenant-[a-z0-9-]+", tenant_id):
        raise NebiusError("The dedicated Nebius profile has no selected tenant")
    project = run_cli(["iam", "project", "get", project_id, "--format", "json"])
    metadata = project.get("metadata", {})
    spec = project.get("spec", {})
    region = spec.get("region") or project.get("status", {}).get("region") or ""
    if not region:
        raise NebiusError("Could not determine the selected project's region")
    return {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "project_name": str(metadata.get("name") or project_id),
        "region": str(region),
    }


def _write_operation(phase: str, stage: str, message: str, **details: Any) -> None:
    import nebius_timing as timing
    job_id = os.environ.get("NEBIUS_JOB_ID", "")
    previous = current_operation()
    if not previous and re.fullmatch(r"[a-f0-9]{24}", job_id):
        previous = {"started_at": _read_json(STATE_DIR / "jobs" / (job_id + ".json"), {}).get("started_at")}
    if not job_id and phase == "running" and previous.get("phase") != "running":
        previous = {}
    if job_id and "cloud_operations" not in details:
        details["cloud_operations"] = previous.get("cloud_operations", [])
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    value = {"schema": "nebius.omarchy-operation/v1", "phase": phase, "stage": stage,
             "message": message, "updated_at": now, **details,
             **timing.advance(previous, phase, stage, now)}
    _atomic_json(OPERATION_FILE, value)
    if re.fullmatch(r"[a-f0-9]{24}", job_id):
        _atomic_json(STATE_DIR / "jobs" / (job_id + ".operation.json"), value)


def current_operation():
    job_id = os.environ.get("NEBIUS_JOB_ID", "")
    path = STATE_DIR / "jobs" / (job_id + ".operation.json") if re.fullmatch(r"[a-f0-9]{24}", job_id) else OPERATION_FILE
    return _read_json(path, {})


def _items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        return []
    return [item for item in value["items"] if isinstance(item, dict)]


def _allocation(status: dict[str, Any], name: str) -> dict[str, Any]:
    value = status.get(name, {}) if isinstance(status, dict) else {}
    level = str(value.get("availability_level") or "AVAILABILITY_LEVEL_UNKNOWN")
    return {
        "available": value.get("available"),
        "limit": value.get("limit"),
        "level": level.removeprefix("AVAILABILITY_LEVEL_").lower(),
        "data_state": str(value.get("data_state") or "DATA_STATE_UNKNOWN").removeprefix("DATA_STATE_").lower(),
        "effective_at": value.get("effective_at"),
    }


def _project_record(item: dict[str, Any], tenant_id: str, tenant_name: str) -> dict[str, Any]:
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    project_id = str(metadata.get("id") or "")
    region = str(spec.get("region") or status.get("region") or "")
    if not region and project_id:
        full = run_cli(["iam", "project", "get", project_id, "--format", "json"])
        region = str(full.get("spec", {}).get("region") or full.get("status", {}).get("region") or "")
    return {
        "tenant_id": tenant_id,
        "tenant_name": tenant_name,
        "project_id": project_id,
        "project_name": str(metadata.get("name") or project_id),
        "region": region,
        "platforms": [],
        "subnet_id": "",
        "subnet_name": "",
        "labels": metadata.get("labels", {}) or {},
    }


def _tenant_user_id(tenant_id: str) -> str:
    identity = run_cli(["iam", "whoami", "--format", "json"], timeout=30)
    for tenant in identity.get("user_profile", {}).get("tenants", []):
        if isinstance(tenant, dict) and tenant.get("tenant_id") == tenant_id:
            subject_id = str(tenant.get("tenant_user_account_id") or "")
            if re.fullmatch(r"tenantuseraccount-[a-z0-9-]+", subject_id):
                return subject_id
    raise NebiusError("Could not identify your account inside the selected tenant")


def _personal_project_registry() -> dict[str, Any]:
    value = _read_json(PROJECTS_FILE, {})
    if not isinstance(value, dict) or value.get("schema") != "nebius.omarchy-projects/v1" or not isinstance(value.get("projects"), list):
        return {"schema": "nebius.omarchy-projects/v1", "projects": []}
    return value


def _save_personal_projects(tenant_id: str, subject_id: str, projects: list[dict[str, Any]]) -> None:
    unique: dict[str, dict[str, Any]] = {}
    for project in projects:
        project_id = str(project.get("project_id") or "")
        if re.fullmatch(r"project-[a-z0-9-]+", project_id):
            unique[project_id] = project
    _atomic_json(
        PROJECTS_FILE,
        {
            "schema": "nebius.omarchy-projects/v1",
            "tenant_id": tenant_id,
            "subject_id": subject_id,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "projects": sorted(
                unique.values(),
                key=lambda row: (str(row.get("project_name", "")).lower(), row["project_id"]),
            ),
        },
    )


def sync_personal_projects(
    tenant_id: str | None = None,
    accessible_projects: list[dict[str, Any]] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    tenant_id = tenant_id or profile_value("tenant-id")
    if not re.fullmatch(r"tenant-[a-z0-9-]+", tenant_id):
        raise NebiusError("No tenant is selected for Nebius")
    subject_id = _tenant_user_id(tenant_id)
    registry = _personal_project_registry()
    registry_matches = registry.get("tenant_id") == tenant_id and registry.get("subject_id") == subject_id

    if accessible_projects is None:
        tenant = run_cli(["iam", "tenant", "get", tenant_id, "--format", "json"], timeout=20)
        tenant_name = str(tenant.get("metadata", {}).get("name") or tenant_id)
        accessible_projects = [
            _project_record(item, tenant_id, tenant_name)
            for item in _items(
                run_cli(
                    ["iam", "project", "list", "--parent-id", tenant_id, "--all", "--format", "json"],
                    timeout=30,
                )
            )
        ]

    accessible_by_id = {str(project.get("project_id")): project for project in accessible_projects}
    sources: dict[str, str] = {}
    if registry_matches:
        for project in registry["projects"]:
            project_id = str(project.get("project_id") or "")
            if project_id in accessible_by_id:
                sources[project_id] = str(project.get("source") or "saved")

    try:
        preferred_id = profile_value("parent-id")
    except NebiusError:
        preferred_id = ""
    if preferred_id in accessible_by_id:
        sources.setdefault(preferred_id, "preferred")
    for project_id, project in accessible_by_id.items():
        if (project.get("labels", {}).get("managed-by") == MANAGED_BY
                and project.get("labels", {}).get("created-by") == subject_id):
            sources[project_id] = "plugin"

    if force or not registry_matches:
        audit_start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)).strftime("%Y-%m-%dT00:00:00Z")
        audit_end = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        audit_filter = (
            f"authentication.subject.tenant_user_id = '{subject_id}' "
            "AND type = 'ai.nebius.iam.project.create'"
        )
        try:
            events = run_cli(
                [
                    "audit", "v2", "audit-event", "list",
                    "--parent-id", tenant_id,
                    "--region", "eu-north1",
                    "--start", audit_start,
                    "--end", audit_end,
                    "--event-type", "control_plane",
                    "--filter", audit_filter,
                    "--all", "--format", "json",
                ],
                timeout=90,
            )
            for event in _items(events):
                if str(event.get("status") or "").upper() != "DONE":
                    continue
                project_id = str(event.get("resource", {}).get("metadata", {}).get("id") or "")
                if project_id in accessible_by_id:
                    sources[project_id] = "created-by-you"
        except NebiusError:
            # Audit access or retention may be limited. Preferred and plugin-created
            # projects remain safe, deterministic fallbacks.
            pass

    personal = []
    for project_id, source in sources.items():
        project = accessible_by_id[project_id]
        personal.append(
            {
                "project_id": project_id,
                "project_name": project.get("project_name") or project_id,
                "region": project.get("region") or "",
                "source": source,
            }
        )
    _save_personal_projects(tenant_id, subject_id, personal)
    return {
        "tenant_id": tenant_id,
        "subject_id": subject_id,
        "projects": personal,
        "hidden_shared_project_count": max(0, len(accessible_by_id) - len(personal)),
    }


def _discover_gpu_capacity() -> dict[str, Any]:
    from nebius_catalog import gpu_name
    tenants_value = run_cli(["iam", "tenant", "list", "--all", "--format", "json"], timeout=20)
    selected_tenant_id = profile_value("tenant-id")
    if not re.fullmatch(r"tenant-[a-z0-9-]+", selected_tenant_id):
        raise NebiusError("No tenant is selected for Nebius; run setup and choose a tenant")
    tenants: list[dict[str, str]] = []
    projects: list[dict[str, Any]] = []
    personal_ids: set[str] = set()
    advice_by_tenant: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []

    for tenant_item in _items(tenants_value):
        metadata = tenant_item.get("metadata", {})
        tenant_id = str(metadata.get("id") or "")
        if tenant_id != selected_tenant_id:
            continue
        tenant_name = str(metadata.get("name") or tenant_id)
        tenants.append({"tenant_id": tenant_id, "tenant_name": tenant_name})
        try:
            project_items = _items(
                run_cli(
                    ["iam", "project", "list", "--parent-id", tenant_id, "--all", "--format", "json"],
                    timeout=20,
                )
            )
        except NebiusError as error:
            errors.append({"scope": tenant_id, "message": str(error)})
            continue
        discovered_projects: list[dict[str, Any]] = []
        for project_item in project_items:
            try:
                project = _project_record(project_item, tenant_id, tenant_name)
            except NebiusError as error:
                errors.append({"scope": str(project_item.get("metadata", {}).get("id") or tenant_id), "message": str(error)})
                continue
            if not project["project_id"] or not project["region"]:
                continue
            discovered_projects.append(project)

        personal = sync_personal_projects(tenant_id, discovered_projects)
        personal_ids = {project["project_id"] for project in personal["projects"]}
        for project in discovered_projects:
            project["personal"] = project["project_id"] in personal_ids

        def load_capabilities(
            project: dict[str, Any],
        ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
            platform_rows = _items(
                run_cli(
                    [
                        "compute",
                        "platform",
                        "list",
                        "--parent-id",
                        project["project_id"],
                        "--all",
                        "--format",
                        "json",
                    ],
                    timeout=25,
                )
            )
            subnet_rows = _items(
                run_cli(
                    ["vpc", "subnet", "list", "--parent-id", project["project_id"], "--all", "--format", "json"],
                    timeout=25,
                )
            )
            return project, platform_rows, subnet_rows

        with ThreadPoolExecutor(max_workers=min(8, max(1, len(discovered_projects)))) as executor:
            pending = {executor.submit(load_capabilities, project): project for project in discovered_projects}
            for future in as_completed(pending):
                project = pending[future]
                try:
                    project, platform_items, subnet_items = future.result()
                except NebiusError as error:
                    errors.append({"scope": project["project_id"], "message": str(error)})
                    continue
                project["preemptible_platforms"] = {
                    str(row.get("metadata", {}).get("name")): row.get("status", {}).get("allowed_for_preemptibles")
                    for row in platform_items
                }
                project["platforms"] = sorted(
                    str(row.get("metadata", {}).get("name"))
                    for row in platform_items
                    if str(row.get("metadata", {}).get("name", "")).startswith("gpu-")
                )
                ready_subnets = [
                    row for row in subnet_items if str(row.get("status", {}).get("state", "")).upper() == "READY"
                ]
                if ready_subnets:
                    project["subnet_id"] = str(ready_subnets[0].get("metadata", {}).get("id") or "")
                    project["subnet_name"] = str(ready_subnets[0].get("metadata", {}).get("name") or "")
                projects.append(project)
        try:
            advice_by_tenant[tenant_id] = _items(
                run_cli(
                    ["capacity", "resource-advice", "list", "--parent-id", tenant_id, "--all", "--format", "json"],
                    timeout=30,
                )
            )
        except NebiusError as error:
            errors.append({"scope": tenant_id, "message": str(error)})
            advice_by_tenant[tenant_id] = []

    if not tenants:
        raise NebiusError("The selected Nebius tenant is no longer accessible; run setup and choose another tenant")

    offerings: list[dict[str, Any]] = []
    for tenant in tenants:
        tenant_id = tenant["tenant_id"]
        for item in advice_by_tenant.get(tenant_id, []):
            spec = item.get("spec", {})
            compute = spec.get("compute_instance", {})
            preset = compute.get("preset", {})
            resources = preset.get("resources", {})
            platform = str(compute.get("platform") or "")
            region = str(spec.get("region") or "")
            compatible = [
                {
                    **{key: project[key]
                    for key in (
                        "tenant_id",
                        "tenant_name",
                        "project_id",
                        "project_name",
                        "region",
                        "subnet_id",
                        "subnet_name",
                    )},
                    "allowed_for_preemptibles": project.get("preemptible_platforms", {}).get(platform),
                }
                for project in projects
                if project["tenant_id"] == tenant_id
                and project["region"] == region
                and platform in project["platforms"]
                and project["subnet_id"]
            ]
            eligible = [project for project in compatible if project["project_id"] in personal_ids]
            eligible.sort(key=lambda project: (str(project["project_name"]).lower(), project["project_id"]))
            if not platform.startswith("gpu-"):
                continue
            identity = "|".join(
                [tenant_id, region, str(spec.get("fabric") or ""), platform, str(preset.get("name") or "")]
            )
            offerings.append(
                {
                    "offering_id": hashlib.sha256(identity.encode()).hexdigest()[:20],
                    "tenant_id": tenant_id,
                    "tenant_name": tenant["tenant_name"],
                    "region": region,
                    "fabric": spec.get("fabric"),
                    "platform": platform,
                    "gpu_label": gpu_name(platform),
                    "preset": preset.get("name"),
                    "gpu_count": resources.get("gpu_count"),
                    "vcpu_count": resources.get("vcpu_count"),
                    "memory_gib": resources.get("memory_gibibytes"),
                    "gpu_memory_gb": compute.get("gpu_memory_gigabytes"),
                    "reserved": _allocation(item.get("status", {}), "reserved"),
                    "on_demand": _allocation(item.get("status", {}), "on_demand"),
                    "preemptible": _allocation(item.get("status", {}), "preemptible"),
                    "projects": eligible,
                    "project_setup_required": not eligible,
                }
            )
    offerings.sort(
        key=lambda row: (
            int(row.get("gpu_count") or 0),
            -_availability_score(row["preemptible"])[0],
            -_availability_score(row["preemptible"])[1],
            str(row["platform"]),
            str(row["region"]),
        )
    )
    projects.sort(key=lambda project: (str(project["project_name"]).lower(), project["project_id"]))
    personal_projects = [project for project in projects if project.get("personal")]
    clean_projects = [
        {key: value for key, value in project.items() if key not in {"platforms", "preemptible_platforms", "labels", "personal"}}
        for project in personal_projects
    ]
    result = {
        "schema": "nebius.omarchy-capacity/v3",
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "live",
        "cache_age_seconds": 0,
        "tenants": tenants,
        "projects": clean_projects,
        "accessible_project_count": len(projects),
        "hidden_shared_project_count": max(0, len(projects) - len(personal_projects)),
        "offerings": offerings,
        "errors": errors,
    }
    _atomic_json(CAPACITY_FILE, result)
    return result


def gpu_capacity(*, force_refresh: bool = False) -> dict[str, Any]:
    cached = _read_json(CAPACITY_FILE, None)
    has_cache = isinstance(cached, dict) and cached.get("schema") == "nebius.omarchy-capacity/v3"
    cache_age = max(0, int(time.time() - CAPACITY_FILE.stat().st_mtime)) if has_cache else 0
    if has_cache and not force_refresh:
        snapshot = dict(cached)
        snapshot["source"] = "cache"
        snapshot["cache_age_seconds"] = cache_age
        snapshot.pop("refresh_error", None)
        return snapshot
    try:
        return _discover_gpu_capacity()
    except NebiusError as error:
        if not has_cache:
            raise
        snapshot = dict(cached)
        snapshot["source"] = "stale-cache"
        snapshot["cache_age_seconds"] = cache_age
        snapshot["refresh_error"] = str(error)
        return snapshot


def preflight_vm(
    region: str,
    allocation: str = "on_demand",
    platform: str = "",
    gpu_count: int = 1,
    *,
    project_id: str | None = None,
    disk_gib: int = DEFAULT_DISK_GIB,
    preset: str = "",
    subnet_id: str = "",
    vm_name: str = "",
    image_family: str = "",
    image_id: str = "",
) -> dict[str, Any]:
    """Read-only admission checks. Capacity advice is not project eligibility."""
    if allocation not in {"on_demand", "preemptible"} or disk_gib < 0 or gpu_count < 1:
        raise NebiusError("Invalid allocation or resource size")
    tenant_id = profile_value("tenant-id")
    if not re.fullmatch(r"tenant-[a-z0-9-]+", tenant_id):
        raise NebiusError("Choose a tenant in Account setup first")
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    strict = bool(project_id and platform)
    gpu_quota = ""

    def check(label: str, ok: bool, message: str) -> None:
        checks.append({"name": label, "state": "ok" if ok else "blocked", "message": message})

    if strict:
        try:
            capability = run_cli(["compute", "platform", "get-by-name", "--parent-id", project_id,
                                  "--name", platform, "--format", "json"], timeout=25)
            spec = capability.get("spec", {})
            check("Platform", capability.get("metadata", {}).get("name") == platform,
                  f"Platform {platform} must be accessible in this project")
            if allocation == "preemptible":
                allowed = capability.get("status", {}).get("allowed_for_preemptibles") is True
                check("Preemptible eligibility", allowed,
                      "Preemptible use is allowed" if allowed else
                      f"{platform} does not allow preemptible VMs in this project. Choose on-demand or another GPU/project")
            else:
                gpu_quota = str(spec.get("gpu_count_quota_type") or "")
            if preset:
                match = next((row for row in spec.get("presets", []) if row.get("name") == preset), None)
                check("GPU configuration", bool(match and int(match.get("resources", {}).get("gpu_count") or 0) == gpu_count),
                      f"{preset} must be a supported {gpu_count}-GPU configuration")
                advice = _items(run_cli(["capacity", "resource-advice", "list", "--parent-id", tenant_id,
                                         "--all", "--format", "json"], timeout=30))
                matches = [row for row in advice if row.get("spec", {}).get("region") == region
                           and row.get("spec", {}).get("compute_instance", {}).get("platform") == platform
                           and row.get("spec", {}).get("compute_instance", {}).get("preset", {}).get("name") == preset]
                capacity = [_allocation(row.get("status", {}), allocation) for row in matches]
                if capacity and all(_availability_score(row)[0] < 0 for row in capacity):
                    check("Live capacity", False, "No capacity is currently reported for this allocation and configuration. Choose another GPU or region")
                elif not capacity or not any(_availability_score(row)[0] > 0 for row in capacity):
                    warnings.append("Live capacity is unreported; Nebius can only confirm it at submission.")
                else:
                    check("Live capacity", True, "Capacity is currently reported; it is not reserved")
            if image_id or image_family:
                if image_id:
                    from nebius_catalog import get_image, compatibility
                    shape = {"platform": platform, "preset": preset, "region": region}
                    image = get_image(image_id, shape)
                    _, notes = compatibility(image, shape)
                    warnings.extend(notes)
                else:
                    catalog = capability.get("metadata", {}).get("parent_id")
                    if not catalog:
                        raise NebiusError("The platform's image catalog could not be identified")
                    image = run_cli(["compute", "image", "get-latest-by-family", "--parent-id", catalog,
                                     "--image-family", image_family, "--format", "json"], timeout=25)
                check("Boot image", image.get("status", {}).get("state") == "READY"
                      and int(image.get("status", {}).get("min_disk_size_bytes") or 0) <= disk_gib * 1024**3,
                      "The boot image must be readable, READY and fit the boot disk")
            if subnet_id:
                subnets = _items(run_cli(["vpc", "subnet", "list", "--parent-id", project_id,
                                         "--all", "--format", "json"], timeout=25))
                check("Network", any(row.get("metadata", {}).get("id") == subnet_id
                                     and row.get("status", {}).get("state") == "READY" for row in subnets),
                      "The selected subnet must still be READY in this project")
            if vm_name:
                for kind, name in [("instance", vm_name)] + ([("disk", vm_name + "-boot")] if disk_gib else []):
                    resources = _items(run_cli(["compute", kind, "list", "--parent-id", project_id,
                                               "--all", "--format", "json"], timeout=25))
                    check("Unique " + kind + " name", not any(row.get("metadata", {}).get("name") == name for row in resources),
                          f"A {kind} named {name} must not already exist. Change the VM name if it does")
        except NebiusError as error:
            check("Project compatibility", False, "Could not verify this configuration: " + str(error).splitlines()[0][:250])
    for parent_id in [tenant_id] + ([project_id] if project_id else []):
        try:
            quotas = _items(run_cli(
                ["quotas", "quota-allowance", "list", "--parent-id", parent_id, "--all", "--format", "json"],
                timeout=25,
            ))
        except NebiusError as error:
            if "expired" in str(error).lower() or "unauthenticated" in str(error).lower():
                raise
            message = f"Quota could not be checked for {parent_id}: {str(error).splitlines()[0][:140]}"
            if strict:
                check("Quota access", False, message)
            else:
                warnings.append(message)
            continue
        found_disk = False
        found_gpu = False
        for quota in quotas:
            metadata, spec, status = (quota.get(key, {}) for key in ("metadata", "spec", "status"))
            if spec.get("region") != region:
                continue
            quota_name = str(metadata.get("name") or "")
            requested = None
            label = str(status.get("description") or quota_name)
            if quota_name == "compute.disk.size.network-ssd":
                found_disk = True
                requested = disk_gib * 1024**3
                label = "SSD storage"
            elif gpu_quota and quota_name == gpu_quota:
                found_gpu = True
                requested = gpu_count
                label = "GPU quota"
            # Other exact quota names are compared only when defined by the API.
            if requested is None:
                continue
            if spec.get("limit") is None:
                checks.append({"name": label, "state": "inherited", "parent_id": parent_id})
                continue
            limit = int(spec["limit"])
            frozen = str(status.get("state")) in {"STATE_FROZEN", "STATE_DELETED"}
            if status.get("usage_state") == "USAGE_STATE_UNKNOWN" and limit >= requested and not frozen:
                message = f"{label} usage is unknown in {region}; refresh or check the Nebius console before creating."
                if strict and requested:
                    check(label, False, message)
                else:
                    warnings.append(message)
                continue
            usage = int(status.get("usage") or 0)
            remaining = max(0, limit - usage)
            blocked = remaining < requested or frozen
            checks.append({
                "name": label, "quota_name": quota_name, "parent_id": parent_id,
                "state": "blocked" if blocked else "ok", "limit": limit,
                "usage": usage, "available": remaining, "requested": requested,
                "message": (f"{label}: {remaining // 1024**3} GiB quota available; {disk_gib} GiB new storage needed"
                            if quota_name == "compute.disk.size.network-ssd" else
                            f"{label}: {remaining} GPUs available; {gpu_count} needed"),
            })
        if not found_disk and parent_id == tenant_id:
            message = f"SSD quota was not reported for {region}; availability is unverified."
            if strict and disk_gib:
                check("SSD quota", False, message)
            else:
                warnings.append(message)
        if strict and allocation == "on_demand" and parent_id == tenant_id and not found_gpu:
            check("GPU quota", False, "GPU quota was not reported; check your project or refresh before creating")
    if strict:
        warnings.append("These checks do not reserve GPUs or prove create permissions. Nebius makes the final admission decision at submission.")
    blocked = [check for check in checks if check["state"] == "blocked"]
    return {
        "ready": not blocked, "region": region, "allocation": allocation,
        "checks": checks, "warnings": warnings,
        "message": blocked[0]["message"] if blocked else "Preflight checks passed" if strict else "Storage quota checked" if checks else "Quota not verified",
        "recovery": "Adjust the allocation, GPU, project or name; for quota/access issues use the Nebius console, then retry." if blocked else "",
    }


def _require_preflight(value: dict[str, Any]) -> None:
    if not value["ready"]:
        raise NebiusError(f"Preflight failed in {value['region']}. {value['message']}. {value['recovery']} No new disk or VM was created.")


@cloud_mutation
def create_nebius_project(
    region: str,
    *,
    name: str | None = None,
    confirmed: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z]{2}-[a-z]+[0-9]+", region):
        raise NebiusError("Invalid Nebius region")
    capacity = gpu_capacity()
    if region not in {str(offering.get("region") or "") for offering in capacity.get("offerings", [])}:
        raise NebiusError("That region is not present in the current GPU capacity snapshot")
    tenant_id = profile_value("tenant-id")
    project_name = _safe_name(name or f"gpu-{region}")
    preview = {
        "tenant_id": tenant_id,
        "region": region,
        "project_name": project_name,
        "creates": ["Nebius project", "default private network", "default subnet"],
        "note": "The project remains if VM creation is canceled later.",
    }
    if dry_run:
        return {"dry_run": True, **preview}
    if confirmed is not True:
        raise NebiusError("Project creation requires an explicit confirmation")
    _require_preflight(preflight_vm(region))
    subject_id = _tenant_user_id(tenant_id)

    existing = run_cli(
        ["iam", "project", "list", "--parent-id", tenant_id, "--all", "--format", "json"], timeout=30
    )
    existing_names = {str(item.get("metadata", {}).get("name") or "") for item in _items(existing)}
    if project_name in existing_names:
        raise NebiusError(f"Project '{project_name}' already exists. Choose that project or edit the name.")

    preview["project_name"] = project_name
    _write_operation("running", "project", f"Creating {project_name} in {region}", **preview)
    created = run_cli(
        [
            "iam", "project", "create",
            "--parent-id", tenant_id,
            "--name", project_name,
            "--region", region,
            "--labels", f"managed-by={MANAGED_BY},created-by={subject_id}",
            "--format", "json",
        ],
        timeout=300,
    )
    project_id = _resource_id(created, "project")
    registry = _personal_project_registry()
    saved = list(registry.get("projects", [])) if (
        registry.get("tenant_id") == tenant_id and registry.get("subject_id") == subject_id
    ) else []
    saved.append({"project_id": project_id, "project_name": project_name, "region": region, "source": "plugin"})
    _save_personal_projects(tenant_id, subject_id, saved)
    _write_operation("running", "network", f"{project_name} exists; waiting for its network",
                     project_id=project_id, project_name=project_name, region=region)
    deadline = time.monotonic() + 300
    subnet_id = ""
    subnet_name = ""
    while time.monotonic() < deadline:
        subnets = _items(
            run_cli(
                ["vpc", "subnet", "list", "--parent-id", project_id, "--all", "--format", "json"],
                timeout=30,
            )
        )
        ready = [item for item in subnets if str(item.get("status", {}).get("state") or "").upper() == "READY"]
        if ready:
            subnet_id = str(ready[0].get("metadata", {}).get("id") or "")
            subnet_name = str(ready[0].get("metadata", {}).get("name") or "")
            break
        time.sleep(4)
    if not subnet_id:
        _write_operation(
            "error",
            "project",
            "Project created, but its default subnet is not ready yet",
            project_id=project_id,
            project_name=project_name,
            region=region,
            recovery="The project remains. Retry Get a GPU VM in a few minutes.",
        )
        raise NebiusError("Project created, but its default subnet is not ready yet. Retry in a few minutes.")

    try:
        CAPACITY_FILE.unlink()
    except FileNotFoundError:
        pass
    result = {
        **preview,
        "project_id": project_id,
        "subnet_id": subnet_id,
        "subnet_name": subnet_name,
        "created": True,
    }
    _write_operation("ready", "project", f"{project_name} is ready in {region}", **result)
    return result


def _availability_score(allocation: dict[str, Any]) -> tuple[int, int]:
    available = allocation.get("available")
    if isinstance(available, int):
        return (4 if available > 0 else -1, available)
    return ({"high": 3, "medium": 2, "low": 1, "limit_reached": -1}.get(str(allocation.get("level")), 0), 0)


def _hourly_estimate(platform: str, gpu_count: int, vcpu_count: int, memory_gib: int,
                     allocation: str = "preemptible", disk_gib: int = DEFAULT_DISK_GIB) -> float | None:
    gpu_price = (PREEMPTIBLE_GPU_USD if allocation == "preemptible" else ON_DEMAND_GPU_USD).get(platform)
    if gpu_price is None:
        return None
    compute = gpu_price * gpu_count
    multiplier = 1 if allocation == "preemptible" else 2
    if platform == "gpu-l40s-a":
        compute += multiplier * (0.006 * vcpu_count + 0.0016 * memory_gib)
    elif platform == "gpu-l40s-d":
        compute += multiplier * (0.005 * vcpu_count + 0.0016 * memory_gib)
    disk = disk_gib * DISK_USD_PER_GIB_MONTH / 730
    return round(compute + disk, 3)


def _safe_name(value: str | None) -> str:
    if value:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,48}[a-z0-9]|[a-z]", value):
            raise NebiusError("Use 1–50 lowercase letters, digits or hyphens; start with a letter and end with a letter or digit.")
        return value
    return "gpu-" + dt.datetime.now(dt.timezone.utc).strftime("%m%d-%H%M%S")


def plan_gpu_vm(
    name: str | None = None,
    offering_id: str | None = None,
    project_id: str | None = None,
    allocation: str = "on_demand",
    auto_stop_hours: int = 0,
    image_id: str = "",
    disk_gib: int | None = None,
) -> dict[str, Any]:
    if allocation not in {"preemptible", "on_demand"}:
        raise NebiusError("Choose on_demand or preemptible allocation")
    if auto_stop_hours != 0:
        raise NebiusError("Auto-stop has been removed. Reopen the manager and review a new plan. VMs run until stopped manually.")
    if disk_gib is not None and (type(disk_gib) is not int or not 50 <= disk_gib <= 30720):
        raise NebiusError("Boot disk size must be between 50 and 30720 GiB")
    capacity = gpu_capacity()
    candidates = [
        offering
        for offering in capacity["offerings"]
        if int(offering.get("gpu_count") or 0) > 0 and _availability_score(offering[allocation])[0] >= 0
    ]
    if not candidates:
        raise NebiusError(f"No GPU option is reported for {allocation.replace('_', '-')} allocation. Switch allocation or refresh capacity.")
    if offering_id:
        candidates = [candidate for candidate in candidates if candidate.get("offering_id") == offering_id]
        if not candidates:
            raise NebiusError("That GPU option is no longer available; refresh capacity and choose again")
    candidates.sort(key=lambda row: (int(row.get("gpu_count") or 0), -_availability_score(row[allocation])[0]))
    selected = candidates[0]
    eligible_projects = selected.get("projects") or []
    if not eligible_projects:
        raise NebiusError(
            f"No personal project is ready in {selected.get('region')}; create one before planning this VM"
        )
    if project_id:
        eligible_projects = [project for project in eligible_projects if project.get("project_id") == project_id]
        if not eligible_projects:
            raise NebiusError("The selected project cannot launch this GPU option")
    else:
        try:
            preferred_project = profile_value("parent-id")
        except NebiusError:
            preferred_project = ""
        eligible_projects.sort(
            key=lambda project: (
                project.get("project_id") != preferred_project,
                str(project.get("project_name") or "").lower(),
            )
        )
        if len(eligible_projects) > 1 and eligible_projects[0].get("project_id") != preferred_project:
            names = ", ".join(str(project.get("project_name")) for project in eligible_projects[:5])
            suffix = "…" if len(eligible_projects) > 5 else ""
            raise NebiusError(f"Choose a compatible project for this GPU option: {names}{suffix}")
    project = eligible_projects[0]
    if not project.get("subnet_id"):
        raise NebiusError("The selected project has no READY subnet")
    vm_name = _safe_name(name)
    created_at = dt.datetime.now(dt.timezone.utc)
    expires_at = created_at + dt.timedelta(minutes=10)
    gpu_count = int(selected.get("gpu_count") or 1)
    boot_image = {"label": IMAGE_FAMILY, "note": "Public Ubuntu / CUDA image family"}
    if image_id:
        from nebius_catalog import get_image, image_row
        image = get_image(image_id, selected)
        details = image_row(image, [selected], image["metadata"]["parent_id"])
        minimum = details["min_disk_gib"]
        if disk_gib is None:
            disk_gib = max(DEFAULT_DISK_GIB, minimum)
        if disk_gib < minimum or disk_gib > 30720:
            raise NebiusError(f"This image requires at least {minimum} GiB of boot disk storage")
        boot_image = {"label": details["name"], "image_id": image_id,
                      "note": "; ".join(details["warnings"]) or "Declared compatible by image metadata"}
    disk_gib = disk_gib if disk_gib is not None else DEFAULT_DISK_GIB
    plan = {
        "schema": "nebius.omarchy-plan/v1",
        "plan_id": secrets.token_urlsafe(18),
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "project": project,
        "offering_id": selected["offering_id"],
        "name": vm_name,
        "platform": selected["platform"],
        "preset": selected["preset"],
        "fabric": selected["fabric"],
        "gpu_count": gpu_count,
        "vcpu_count": int(selected.get("vcpu_count") or 0),
        "memory_gib": int(selected.get("memory_gib") or 0),
        "allocation": allocation,
        "capacity": selected[allocation],
        "subnet_id": project["subnet_id"],
        "subnet_name": project["subnet_name"],
        "image_family": "" if image_id else IMAGE_FAMILY,
        "image_id": image_id,
        "boot_image": boot_image,
        "disk_gib": disk_gib,
        "ssh_user": SSH_USER,
        "ssh_public_key": str(SSH_KEY.with_suffix(".pub")),
        "network_note": "Static public IPv4; inbound TCP 22 only. Outbound traffic is allowed. Use Ports for local application access.",
        "runtime_note": "No automatic stop is scheduled. Stop the VM manually when finished; disks remain billable until deleted.",
        "estimated_usd_per_hour": _hourly_estimate(
            str(selected["platform"]),
            gpu_count,
            int(selected.get("vcpu_count") or 0),
            int(selected.get("memory_gib") or 0),
            allocation,
            disk_gib,
        ),
        "pricing_url": PRICING_URL,
        "pricing_checked_at": PRICING_CHECKED_AT,
        "pricing_note": "Estimate excludes public-IP traffic and taxes; the disk remains billable while stopped.",
    }
    plan["reusable_disk"] = _select_reusable_disk(plan)
    plan["preflight"] = preflight_vm(
        project["region"], allocation, str(selected["platform"]), gpu_count, project_id=project["project_id"],
        disk_gib=0 if plan["reusable_disk"] else plan["disk_gib"],
        preset=plan["preset"], subnet_id=plan["subnet_id"], vm_name=vm_name,
        image_family="" if plan["reusable_disk"] else plan["image_family"],
        image_id="" if plan["reusable_disk"] else plan["image_id"],
    )
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    PLAN_DIR.chmod(0o700)
    _atomic_json(PLAN_DIR / f"{plan['plan_id']}.json", plan)
    return plan


def ensure_ssh_key() -> None:
    public_key = SSH_KEY.with_suffix(".pub")
    if SSH_KEY.is_file() and public_key.is_file():
        return
    SSH_KEY.parent.mkdir(parents=True, exist_ok=True)
    SSH_KEY.parent.chmod(0o700)
    _run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "Omarchy Nebius", "-f", str(SSH_KEY)],
        timeout=30,
    )
    SSH_KEY.chmod(0o600)
    public_key.chmod(0o644)


def _cloud_init() -> str:
    ensure_ssh_key()
    public_key = SSH_KEY.with_suffix(".pub").read_text(encoding="utf-8").strip()
    if not public_key.startswith("ssh-ed25519 "):
        raise NebiusError("The dedicated SSH public key is invalid")
    return (
        "#cloud-config\n"
        "users:\n"
        f"  - name: {SSH_USER}\n"
        "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        "    shell: /bin/bash\n"
        "    ssh_authorized_keys:\n"
        f"      - {public_key}\n"
    )


def _resource_id(value: Any, kind: str) -> str:
    candidates = [
        value.get("metadata", {}).get("id") if isinstance(value, dict) else None,
        value.get("resource", {}).get("metadata", {}).get("id") if isinstance(value, dict) else None,
        value.get("result", {}).get("metadata", {}).get("id") if isinstance(value, dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith(kind + "-"):
            return candidate
    raise NebiusError(f"Nebius did not return a {kind} resource ID")


def _registry() -> dict[str, Any]:
    value = _read_json(REGISTRY_FILE, {"schema": "nebius.omarchy-vms/v1", "vms": []})
    if not isinstance(value, dict) or not isinstance(value.get("vms"), list):
        return {"schema": "nebius.omarchy-vms/v1", "vms": []}
    return value


def _save_registry(value: dict[str, Any]) -> None:
    _atomic_json(REGISTRY_FILE, value)


def _update_vm_record(vm_id, changes=None, *, remove=False):
    with mutation_guard(wait=True, resource="registry"):
        registry = _registry()
        previous = next((vm for vm in registry["vms"] if vm.get("id") == vm_id), {})
        registry["vms"] = [vm for vm in registry["vms"] if vm.get("id") != vm_id]
        if not remove:
            registry["vms"].append({**previous, **(changes or {}), "id": vm_id})
        _save_registry(registry)


def _registered(vm_id: str) -> dict[str, Any]:
    for vm in _registry()["vms"]:
        if isinstance(vm, dict) and vm.get("id") == vm_id:
            return vm
    raise NebiusError("Refusing to operate on a VM that was not created by this plugin")


def _pending_path(request_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", request_id):
        raise NebiusError("Invalid launch request ID")
    return STATE_DIR / "pending" / f"{request_id}.json"


def _pending_launches() -> list[dict[str, Any]]:
    return [value for path in (STATE_DIR / "pending").glob("*.json")
            if isinstance(value := _read_json(path, None), dict) and value.get("plan_id")]


def _reusable_path(disk_id: str) -> Path:
    if not re.fullmatch(r"computedisk-[a-z0-9-]+", disk_id):
        raise NebiusError("Invalid saved disk ID")
    return STATE_DIR / "reusable-disks" / f"{disk_id}.json"


def _reusable_disks() -> list[dict[str, Any]]:
    return [value for path in (STATE_DIR / "reusable-disks").glob("*.json")
            if isinstance(value := _read_json(path, None), dict) and value.get("state") == "available"]


def _verify_reusable_disk(saved: dict[str, Any]) -> dict[str, Any]:
    disk = run_cli(["compute", "disk", "get", saved["disk_id"], "--format", "json"], timeout=25)
    metadata, status = disk.get("metadata", {}), disk.get("status", {})
    labels = metadata.get("labels") or {}
    if (metadata.get("id") != saved["disk_id"] or metadata.get("parent_id") != saved["project"]["project_id"]
            or labels.get("managed-by") != MANAGED_BY or labels.get("request-id") != saved["source_request_id"]):
        raise NebiusError("The saved boot disk's ownership could not be verified; no resources were changed")
    if (str(status.get("state")).upper() != "READY" or status.get("lock_state")
            or status.get("read_write_attachment") or status.get("read_only_attachments")
            or status.get("reconciling") or status.get("managed_by")
            or int(status.get("size_bytes") or 0) != int(saved["disk_gib"]) * 1024**3
            or (saved.get("source_image_id") and status.get("source_image_id") != saved["source_image_id"])):
        raise NebiusError("The saved boot disk is not ready for safe reuse; no resources were changed")
    items = _items(run_cli(["compute", "instance", "list", "--parent-id", saved["project"]["project_id"],
                           "--all", "--format", "json"], timeout=25))
    for vm in items:
        spec, vm_status = vm.get("spec", {}), vm.get("status", {})
        attached = [spec.get("boot_disk", {}), *spec.get("secondary_disks", [])]
        if (any(item.get("existing_disk", {}).get("id") == saved["disk_id"] for item in attached)
                or any(item.get("id") == saved["disk_id"] for item in vm_status.get("disk_attachments", []))
                or (vm.get("metadata", {}).get("labels") or {}).get("request-id") == saved["source_request_id"]):
            raise NebiusError("A VM already uses this launch or disk. Refresh the overview; no resources were changed")
    return disk


def _known_create_rejection(plan: dict[str, Any], error: str) -> bool:
    if _cli_rejected_json(error):
        return True
    # Only the specific synchronous admission rejection seen in production.
    # Generic InvalidArgument, transport errors and timeouts remain uncertain.
    return (plan.get("allocation") == "preemptible"
            and bool(re.match(r"^(?:Nebius: )?Error: rpc error: code = InvalidArgument desc = Preemptible is invalid\b", error))
            and bool(re.search(r"Preemptible: preemptible instances of platform " + re.escape(plan["platform"])
                               + r" are not allowed in " + re.escape(plan["project"]["project_id"]) + r"(?:\s|$)", error)))


def _select_reusable_disk(plan: dict[str, Any]) -> dict[str, Any] | None:
    for saved in sorted(_reusable_disks(), key=lambda item: item.get("saved_at", "")):
        if (saved["project"]["project_id"] == plan["project"]["project_id"]
                and saved.get("image_family", "") == plan.get("image_family", "")
                and saved.get("image_id", "") == plan.get("image_id", "")
                and saved["disk_gib"] == plan["disk_gib"]):
            _verify_reusable_disk(saved)
            return saved
    return None


def _release_rejected_launch(plan: dict[str, Any], error: str) -> dict[str, Any]:
    """Preserve disk and evidence after a verified rejection; never delete it."""
    saved = plan.get("reusable_disk") or {
        "disk_id": plan["disk_id"], "name": plan["name"] + "-boot", "project": plan["project"],
        "source_request_id": plan["plan_id"], "image_family": plan["image_family"],
        "image_id": plan.get("image_id", ""), "disk_gib": plan["disk_gib"],
    }
    disk = _verify_reusable_disk(saved)
    saved = {**saved, "state": "available", "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
             "source_image_id": disk.get("status", {}).get("source_image_id"), "name": disk["metadata"]["name"]}
    saved.pop("claimed_by", None)
    _atomic_json(_reusable_path(saved["disk_id"]), saved)
    _atomic_json(STATE_DIR / "resolved-requests" / f"{plan['plan_id']}.json",
                 {**plan, "resolution": "rejected_before_submission" if _cli_rejected_json(error) else "admission_rejected", "error": error})
    _pending_path(plan["plan_id"]).unlink()
    return saved


def _rejection_evidence(plan: dict[str, Any]) -> str | None:
    if plan.get("outcome") in {"not_submitted", "admission_rejected"} and _known_create_rejection(plan, plan.get("error", "")):
        return plan["error"]
    # v0.5 saved the original error only in activity.log. Match all resource
    # identifiers; a later unrelated failure must never release another request.
    try:
        with (STATE_DIR / "activity.log").open(encoding="utf-8") as log:
            for line in log:
                try:
                    job = json.loads(line)
                except json.JSONDecodeError:
                    continue
                op = job.get("operation") or {}
                if (job.get("command") == "create" and job.get("phase") == "error"
                        and _known_create_rejection(plan, job.get("error", ""))
                        and op.get("disk_id") == plan.get("disk_id") and op.get("name") == plan.get("name")
                        and op.get("project", {}).get("project_id") == plan["project"]["project_id"]):
                    return job["error"]
    except OSError:
        pass
    return None


@cloud_mutation
def repair_rejected_launches() -> dict[str, Any]:
    resolved, unchanged = [], []
    for plan in _pending_launches():
        evidence = _rejection_evidence(plan)
        if not evidence or not plan.get("disk_id"):
            unchanged.append(plan["plan_id"])
            continue
        resolved.append(_release_rejected_launch(plan, evidence))
    if resolved:
        _write_operation("ready", "repair", "Recovery block cleared. Existing boot disk will be offered for reuse.",
                         disk_ids=[item["disk_id"] for item in resolved],
                         recovery="Choose your GPU and the same project, then review the reused disk before confirming.")
    return {"resolved": resolved, "unchanged": unchanged, "cloud_resources_changed": False}


def _vm_record(plan, vm_id, disk_id):
    return {
        "id": vm_id, "disk_id": disk_id, "name": plan["name"],
        "project_id": plan["project"]["project_id"], "region": plan["project"]["region"],
        "platform": plan["platform"], "preset": plan["preset"], "allocation": plan["allocation"],
        "image_id": plan.get("image_id", ""), "image_family": plan.get("image_family", ""),
        "ssh_user": SSH_USER, "created_at": plan["submitted_at"],
        "source_request_id": (plan.get("reusable_disk") or {}).get("source_request_id", plan["plan_id"]),
    }


@cloud_mutation
def recover_launch(request_id: str) -> dict[str, Any]:
    path = _pending_path(request_id)
    plan = _read_json(path, None)
    if not plan:
        raise NebiusError("No interrupted launch with that request ID")
    project_id = plan["project"]["project_id"]
    personal = sync_personal_projects()
    if project_id not in {p["project_id"] for p in personal["projects"]}:
        raise NebiusError("This request is outside your personal projects")
    evidence = _rejection_evidence(plan)
    if evidence and plan.get("disk_id"):
        saved = _release_rejected_launch(plan, evidence)
        _write_operation("ready", "repair", "VM creation was rejected. Its boot disk is available to reuse or delete.", disk_id=saved["disk_id"])
        return {"rejected": True, "saved_disk": saved, "cloud_resources_changed": False}
    _write_operation("running", "recovery", "Checking the interrupted launch", request_id=request_id)
    items = _items(run_cli(["compute", "instance", "list", "--parent-id", project_id, "--all", "--format", "json"]))
    matches = [item for item in items if item.get("metadata", {}).get("labels", {}).get("request-id") == request_id
               and item.get("metadata", {}).get("labels", {}).get("managed-by") == MANAGED_BY]
    if len(matches) != 1:
        disks = _items(run_cli(["compute", "disk", "list", "--parent-id", project_id, "--all", "--format", "json"]))
        ids = [item["metadata"]["id"] for item in disks
               if item.get("metadata", {}).get("labels", {}).get("request-id") == request_id
               and item.get("metadata", {}).get("labels", {}).get("managed-by") == MANAGED_BY]
        plan["discovered_disk_ids"] = ids
        _atomic_json(path, plan)
        raise NebiusError("The launch outcome is still unconfirmed. "
                         "Check this project in the Nebius console before retrying. "
                         "No resources were deleted. Disks: " + (", ".join(ids) or "none found yet"))
    item = matches[0]
    vm_id = _resource_id(item, "computeinstance")
    disk_id = item.get("spec", {}).get("boot_disk", {}).get("existing_disk", {}).get("id")
    if not disk_id or (plan.get("disk_id") and disk_id != plan["disk_id"]):
        raise NebiusError("Recovered VM's boot disk could not be verified. Inspect the request in the Nebius console.")
    vm = _vm_record(plan, vm_id, disk_id)
    registry = _registry()
    existing = next((entry for entry in registry["vms"] if entry.get("id") == vm_id), None)
    if existing:
        vm = existing
    else:
        _update_vm_record(vm_id, vm)
    for key in ("auto_stop_hours", "auto_stop_at", "auto_stop_warning"):
        vm.pop(key, None)
    _update_vm_record(vm_id, vm)
    if plan.get("reusable_disk"):
        _atomic_json(_reusable_path(disk_id), {**plan["reusable_disk"], "state": "consumed", "vm_id": vm_id})
    path.unlink()
    _write_operation("ready", "recovery", "VM recovered. Open its overview to connect or manage it.",
                     vm_id=vm_id, disk_id=disk_id)
    return vm


@cloud_mutation
def archive_request(request_id: str, confirmed: bool = False) -> dict[str, Any]:
    if confirmed is not True:
        raise NebiusError("Archiving requires confirmation that the cloud outcome was checked")
    path = _pending_path(request_id)
    plan = _read_json(path, None)
    if not plan:
        raise NebiusError("No interrupted launch with that request ID")
    _atomic_json(STATE_DIR / "archived-requests" / path.name, plan)
    path.unlink()
    _write_operation("ready", "recovery", "Request archived locally. No cloud resources were changed or deleted.")
    return {"archived": True, "request_id": request_id, "cloud_resources_changed": False}


def validate_instance_request(request: dict[str, Any]) -> dict[str, Any]:
    """Exercise the pinned CLI's actual ProtoJSON parser with networking disabled.

    unshare is part of Arch's util-linux. The user/network namespaces grant no
    host privileges. There is deliberately no unsandboxed fallback: this create
    command must NEVER reach a cloud endpoint during validation.
    """
    command = [
        "/usr/bin/unshare", "--user", "--map-root-user", "--net", str(CLI),
        "--profile", PROFILE, "--no-check-update", "--no-browser", "--no-progress",
        "--auth-timeout", "1s", "--timeout", "1s", "--per-retry-timeout", "1s", "--retries", "1",
        "compute", "instance", "create", json.dumps(request, separators=(",", ":")), "--format", "json",
    ]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NebiusError(f"Could not validate the VM request safely. No disk or VM was created. {error}") from error
    detail = result.stderr.strip()
    # A parser-success request reaches the transport, which must fail inside
    # the isolated namespace. Other errors (including sandbox/auth failures)
    # are NOT interpreted as validation success.
    if result.returncode and "code = Unavailable" in detail and "network is unreachable" in detail:
        return {"valid": True, "validator": "Nebius CLI ProtoJSON", "network": "disabled"}
    raise NebiusError("VM request validation failed before allocating resources: " + (detail or result.stdout.strip())[:1800])


def _instance_request(plan: dict[str, Any], disk_id: str, *, cloud_init: str | None = None) -> dict[str, Any]:
    request = {
        "metadata": {
            "parent_id": plan["project"]["project_id"],
            "name": plan["name"],
            "labels": {"managed-by": MANAGED_BY, "request-id": plan["plan_id"]},
        },
        "spec": {
            "stopped": False,
            "cloud_init_user_data": _cloud_init() if cloud_init is None else cloud_init,
            "resources": {"platform": plan["platform"], "preset": plan["preset"]},
            "boot_disk": {"attach_mode": "READ_WRITE", "existing_disk": {"id": disk_id}},
            "network_interfaces": [
                {
                    "name": "eth0",
                    "subnet_id": plan["subnet_id"],
                    "ip_address": {},
                    "public_ip_address": {"static": True},
                    "security_groups": [{"id": plan.get("security_group_id", "vpcsecuritygroup-validation")}],
                }
            ],
        },
    }
    if plan["allocation"] == "preemptible":
        request["spec"]["preemptible"] = {"on_preemption": "STOP", "priority": 0}
        request["spec"]["recovery_policy"] = "FAIL"
    else:
        request["spec"]["reservation_policy"] = {"policy": "FORBID"}
    return request


def _ssh_security_group(plan):
    """Attach only a verified SSH-only group; never fall back to the permissive default."""
    subnet = run_cli(["vpc", "subnet", "get", plan["subnet_id"], "--format", "json"])
    network_id = subnet.get("spec", {}).get("network_id")
    if not network_id:
        raise NebiusError("Could not determine the subnet network for SSH-only access")
    rules = [
        {"access": "ALLOW", "protocol": "TCP", "type": "STATEFUL", "priority": 100,
         "ingress": {"source_cidrs": ["0.0.0.0/0"], "destination_ports": [22]}},
        {"access": "ALLOW", "protocol": "ANY", "type": "STATEFUL", "priority": 100,
         "egress": {"destination_cidrs": ["0.0.0.0/0"]}},
    ]
    with mutation_guard(wait=True, resource="network-" + network_id):
        groups = _items(run_cli(["vpc", "security-group", "list", "--parent-id", plan["project"]["project_id"], "--format", "json"]))
        for group in groups:
            if (group.get("metadata", {}).get("labels", {}).get("managed-by") != MANAGED_BY
                    or group.get("spec", {}).get("network_id") != network_id):
                continue
            group_id = group["metadata"]["id"]
            actual = _items(run_cli(["vpc", "security-rule", "list", "--parent-id", group_id, "--format", "json"]))
            if len(actual) == 2 and all(any(item.get("spec") == rule for item in actual) for rule in rules):
                return group_id
        group = run_cli(["vpc", "security-group", "create", json.dumps({
            "metadata": {"parent_id": plan["project"]["project_id"], "name": "omarchy-ssh-" + secrets.token_hex(4),
                         "labels": {"managed-by": MANAGED_BY}}, "spec": {"network_id": network_id}}), "--format", "json"])
        group_id = _resource_id(group, "vpcsecuritygroup")
        for index, rule in enumerate(rules):
            run_cli(["vpc", "security-rule", "create", json.dumps({
                "metadata": {"parent_id": group_id, "name": ["ssh", "outbound"][index]}, "spec": rule}), "--format", "json"])
        return group_id


def _disk_arguments(plan: dict[str, Any]) -> list[str]:
    return [
        "compute",
        "disk",
        "create",
        "--parent-id",
        plan["project"]["project_id"],
        "--name",
        plan["name"] + "-boot",
        "--labels",
        f"managed-by={MANAGED_BY},request-id={plan['plan_id']}",
        "--size-gibibytes",
        str(plan["disk_gib"]),
        "--type",
        "network_ssd",
        "--source-image-id" if plan.get("image_id") else "--source-image-family-image-family",
        plan.get("image_id") or plan["image_family"],
        "--block-size-bytes",
        "4096",
        "--format",
        "json",
    ]


def _wait_for_instance(vm_id: str, *, timeout: int = 720) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_state = "unknown"
    while time.monotonic() < deadline:
        instance = run_cli(["compute", "instance", "get", vm_id, "--format", "json"], timeout=45)
        status = instance.get("status", {})
        last_state = str(status.get("state") or "unknown").lower()
        interfaces = status.get("network_interfaces") or []
        address = ""
        if interfaces and isinstance(interfaces[0], dict):
            address = str(interfaces[0].get("public_ip_address", {}).get("address") or "").split("/")[0]
            if not address:
                address = str(interfaces[0].get("ip_address", {}).get("address") or "").split("/")[0]
        if last_state == "running" and address:
            return {"state": last_state, "public_ip": address}
        if last_state in {"failed", "error", "deleted"}:
            raise NebiusError(f"VM entered state {last_state}")
        time.sleep(5)
    raise NebiusError(f"VM did not become reachable within {timeout} seconds (last state: {last_state})")


def _wait_for_vm_ssh(vm_id: str, name: str, username: str | None = None) -> None:
    import nebius_ssh as ssh_client
    details = {"vm_id": vm_id, "name": name, "ssh_user": username}
    probe = {"attempts": []}
    details["ssh_probe"] = probe
    _write_operation("running", "ssh", "VM is running; waiting for authenticated SSH login", **details)
    last_update = -2
    def progress(message, elapsed):
        nonlocal last_update
        if elapsed - last_update >= 2:
            _write_operation("running", "ssh", message, **details)
            last_update = elapsed
    try:
        prepared = time.monotonic()
        connection = connect_vm(vm_id, launch=False, username=username)
        probe["preparation_seconds"] = round(time.monotonic() - prepared, 3)
        if ssh_client.wait_ready(connection, progress=progress, record=probe["attempts"].append) is not True:
            raise ssh_client.SSHError("SSH login has not been verified")
        _write_operation("running", "ssh", "SSH readiness verified", **details)
    except (ssh_client.SSHError, NebiusError, OSError) as error:
        _write_operation("error", "ssh", "VM is running, but SSH login is not ready", **details,
                         details=str(error), recovery="The VM exists. Check its SSH username, key and network, then retry SSH. Do not create another VM.")
        raise NebiusError("VM is running, but SSH login is not ready: " + str(error)) from error


@cloud_mutation
def create_gpu_vm(plan_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", plan_id):
        raise NebiusError("Invalid plan ID")
    plan_path = PLAN_DIR / f"{plan_id}.json"
    plan = _read_json(plan_path, None)
    if not isinstance(plan, dict) or plan.get("schema") != "nebius.omarchy-plan/v1":
        raise NebiusError("Plan not found; create a fresh plan")
    if plan.get("auto_stop_hours"):
        raise NebiusError("This old plan included auto-stop, which has been removed. Review a new plan before creating a VM without a timer.")
    try:
        expires_at = dt.datetime.fromisoformat(str(plan["expires_at"]))
    except (KeyError, ValueError) as error:
        raise NebiusError("Plan expiry is invalid") from error
    if dt.datetime.now(dt.timezone.utc) >= expires_at:
        raise NebiusError("Plan expired; refresh capacity and create a new plan")
    planned_project = plan.get("project", {})
    project_context(planned_project.get("project_id"), planned_project.get("tenant_id"))

    if dry_run:
        # Preview must never generate keys or submit cloud mutations.
        preview_request = _instance_request(plan, (plan.get("reusable_disk") or {}).get("disk_id", "computedisk-validation"),
                                            cloud_init="#cloud-config\n# SSH key inserted only on confirmed creation\n")
        return {
            "dry_run": True,
            "plan": plan,
            "disk_action": "reuse" if plan.get("reusable_disk") else "create",
            "disk_command": None if plan.get("reusable_disk") else _disk_arguments(plan),
            "instance_request": preview_request,
        }

    if plan.get("submitted_at"):
        raise NebiusError("This plan has already been submitted. Check the overview before starting a new one.")
    if any(item.get("project", {}).get("project_id") == planned_project["project_id"] for item in _pending_launches()):
        raise NebiusError("An earlier launch in this project needs recovery. Open Your VMs before creating another.")
    reusable = plan.get("reusable_disk")
    if reusable:
        current = _read_json(_reusable_path(reusable["disk_id"]), {})
        if current.get("state") != "available" or current.get("source_request_id") != reusable["source_request_id"]:
            raise NebiusError("That saved disk is no longer available. Review a fresh plan before creating.")
        _verify_reusable_disk(reusable)
    _write_operation("running", "preflight", "Checking live eligibility, capacity, network, image, names and quota")
    try:
        plan["preflight"] = preflight_vm(
            planned_project["region"], plan["allocation"], plan["platform"], plan["gpu_count"],
            project_id=planned_project["project_id"], disk_gib=0 if reusable else plan["disk_gib"],
            preset=plan["preset"], subnet_id=plan["subnet_id"], vm_name=plan["name"],
            image_family="" if reusable else plan["image_family"],
            image_id="" if reusable else plan.get("image_id", ""),
        )
        _require_preflight(plan["preflight"])
        ensure_ssh_key()
        request = _instance_request(plan, reusable["disk_id"] if reusable else "computedisk-validation")
        plan["request_validation"] = validate_instance_request(request)
        plan["security_group_id"] = _ssh_security_group(plan)
        request["spec"]["network_interfaces"][0]["security_groups"] = [{"id": plan["security_group_id"]}]
    except (NebiusError, OSError) as error:
        _write_operation("error", "preflight", str(error).splitlines()[0][:300], details=str(error),
                         recovery="No new disk or VM was created. Correct the failed check, then review again.",
                         name=plan["name"], project=planned_project, preflight=plan.get("preflight"))
        raise NebiusError(str(error)) from error
    plan["submitted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _atomic_json(plan_path, plan)
    _atomic_json(_pending_path(plan_id), plan)
    if reusable:
        _atomic_json(_reusable_path(reusable["disk_id"]), {**reusable, "state": "claimed", "claimed_by": plan_id})

    disk_id = ""
    vm_id = ""
    _write_operation(
        "running",
        "disk",
        "Reusing the saved boot disk" if reusable else "Creating the boot disk",
        name=plan["name"],
        project=plan["project"],
        platform=plan["platform"],
        preset=plan["preset"],
    )
    try:
        if reusable:
            disk_id = reusable["disk_id"]
        else:
            disk = run_cli(_disk_arguments(plan), timeout=600)
            disk_id = _resource_id(disk, "computedisk")
        plan["disk_id"] = disk_id
        _atomic_json(_pending_path(plan_id), plan)
        _write_operation(
            "running",
            "instance",
            "Boot disk ready; creating the VM",
            name=plan["name"],
            disk_id=disk_id,
            project=plan["project"],
            platform=plan["platform"],
            preset=plan["preset"],
        )
        request["spec"]["boot_disk"]["existing_disk"]["id"] = disk_id
        instance = run_cli(
            ["compute", "instance", "create", json.dumps(request, separators=(",", ":")), "--format", "json"],
            timeout=900,
        )
        vm_id = _resource_id(instance, "computeinstance")
    except Exception as error:
        rejected = bool(disk_id) and _known_create_rejection(plan, str(error))
        if rejected:
            plan.update(outcome="not_submitted" if _cli_rejected_json(str(error)) else "admission_rejected", error=str(error))
            _atomic_json(_pending_path(plan_id), plan)
            try:
                _release_rejected_launch(plan, str(error))
            except NebiusError:
                # Keep the classification/evidence if the read-only disk check
                # is unavailable; Repair can complete it later.
                pass
        # A timed-out create may complete remotely. Preserve its disk and resource
        # IDs for reconciliation instead of attempting a blind cleanup.
        recovery = ("VM creation was rejected. The disk is kept for reuse or confirmed deletion; repair if verification is unavailable. "
                    if rejected else f"Boot disk exists. Refresh the overview before retrying; the VM request may still finish. Disk: {disk_id}. "
                    if disk_id else "No VM or boot disk was confirmed. ")
        _write_operation(
            "error",
            "create",
            explain_error(str(error))["message"],
            name=plan.get("name"),
            project=plan.get("project"),
            disk_id=disk_id,
            details=str(error),
            recovery=recovery + explain_error(str(error))["recovery"],
        )
        raise

    registry = _registry()
    vm = _vm_record(plan, vm_id, disk_id)
    _update_vm_record(vm_id, vm)
    if reusable:
        _atomic_json(_reusable_path(reusable["disk_id"]), {**reusable, "state": "consumed", "vm_id": vm_id})
    _pending_path(plan_id).unlink()
    _write_operation(
        "running",
        "boot",
        "VM created; waiting for its public address",
        name=plan["name"],
        vm_id=vm_id,
        disk_id=disk_id,
        project=plan["project"],
        platform=plan["platform"],
        preset=plan["preset"],
    )
    try:
        ready = _wait_for_instance(vm_id)
    except NebiusError as error:
        _write_operation(
            "error",
            "boot",
            str(error),
            name=plan["name"],
            vm_id=vm_id,
            disk_id=disk_id,
            project=plan["project"],
            recovery="The VM exists. Open Manage Nebius VMs to inspect, connect, stop, or delete it.",
        )
        raise
    vm.update(ready)
    _update_vm_record(vm_id, vm)
    _wait_for_vm_ssh(vm_id, vm["name"], vm.get("ssh_user"))
    _write_operation(
        "ready",
        "done",
        "SSH login verified; your VM is ready",
        ssh_ready=True,
        ssh_probe=current_operation().get("ssh_probe", {}),
        name=vm["name"],
        vm_id=vm_id,
        disk_id=disk_id,
        public_ip=vm.get("public_ip"),
        project=plan["project"],
        platform=plan["platform"],
        preset=plan["preset"],
    )
    vm.update(ssh_ready=True, launch_timing=current_operation())
    _update_vm_record(vm_id, vm)
    try:
        plan_path.unlink()
    except FileNotFoundError:
        pass
    return vm


def list_managed_vms() -> dict[str, Any]:
    result: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for registered in _registry()["vms"]:
        if not isinstance(registered, dict) or not str(registered.get("id") or "").startswith("computeinstance-"):
            continue
        if registered.get("deletion_only"):
            continue
        vm_id = str(registered["id"])
        try:
            item = run_cli(["compute", "instance", "get", vm_id, "--format", "json"])
        except NebiusError as error:
            errors.append({"id": vm_id, "name": str(registered.get("name") or vm_id), "error": str(error)})
            continue
        metadata = item.get("metadata", {})
        labels = metadata.get("labels", {}) or {}
        if labels.get("managed-by") != MANAGED_BY:
            errors.append({"id": vm_id, "name": str(registered.get("name") or vm_id), "error": "Ownership label missing"})
            continue
        status = item.get("status", {})
        interfaces = status.get("network_interfaces") or []
        address = ""
        if interfaces and isinstance(interfaces[0], dict):
            address = str(interfaces[0].get("public_ip_address", {}).get("address") or "").split("/")[0]
        result.append(
            {
                **registered,
                "id": vm_id,
                "name": metadata.get("name"),
                "state": str(status.get("state") or "unknown").lower(),
                "public_ip": address or registered.get("public_ip"),
                "platform": item.get("spec", {}).get("resources", {}).get("platform"),
                "preset": item.get("spec", {}).get("resources", {}).get("preset"),
            }
        )
    return {"vms": result, "errors": errors}


def ssh_identity_for_instance(item: dict[str, Any]) -> str:
    # Cloud provenance survives reinstall and selects the retained SSH key.
    labels = item.get("metadata", {}).get("labels") or {}
    return "nebius" if labels.get("managed-by") == MANAGED_BY else "default"


def ssh_identity_options(connection: dict[str, Any]) -> list[str]:
    if connection.get("managed"):
        return ["-o", "IdentitiesOnly=yes", "-i", str(SSH_KEY)]
    if connection.get("ssh_identity") == "nebius" and SSH_KEY.is_file():
        # Offer the retained key without excluding an existing agent/config.
        # Never generate a replacement key or adopt a VM merely to log in.
        return ["-i", str(SSH_KEY)]
    return []


def _vm_summary(item: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    metadata, spec, status = (item.get(key, {}) for key in ("metadata", "spec", "status"))
    vm_id = str(metadata.get("id") or "")
    registered = next((vm for vm in _registry()["vms"] if vm.get("id") == vm_id), {})
    interfaces = status.get("network_interfaces") or []
    public_ip = next((str(row.get("public_ip_address", {}).get("address") or "").split("/")[0]
                      for row in interfaces if row.get("public_ip_address", {}).get("address")), "")
    private_ip = next((str(row.get("ip_address", {}).get("address") or "").split("/")[0]
                       for row in interfaces if row.get("ip_address", {}).get("address")), "")
    managed = bool(registered) and not registered.get("deletion_only") and (metadata.get("labels") or {}).get("managed-by") == MANAGED_BY
    recovery_id = next((pending["plan_id"] for pending in _pending_launches()
                        if not registered and pending["plan_id"] == (metadata.get("labels") or {}).get("request-id")
                        and pending["project"]["project_id"] == project["project_id"]
                        and (metadata.get("labels") or {}).get("managed-by") == MANAGED_BY), None)
    resources = spec.get("resources", {})
    connections = _read_json(CONNECTIONS_FILE, {})
    username = connections.get(vm_id, {}).get("username") or registered.get("ssh_user") or ""
    if not username:
        match = re.search(r"^\s*-\s*name:\s*([a-z_][a-z0-9_-]*)\s*$", str(spec.get("cloud_init_user_data") or ""), re.M)
        username = match.group(1) if match else ""
    return {
        **registered, "id": vm_id, "name": str(metadata.get("name") or vm_id),
        "project_id": project["project_id"], "project_name": project["project_name"],
        "region": project["region"], "state": str(status.get("state") or "unknown").lower(),
        "platform": resources.get("platform") or "CPU", "preset": resources.get("preset") or "",
        "allocation": "preemptible" if spec.get("preemptible") else "on_demand",
        "public_ip": public_ip, "private_ip": private_ip, "ssh_user": username,
        "managed": managed, "can_delete": not bool(status.get("managed_by") or spec.get("forbid_deletion")),
        "disk_id": spec.get("boot_disk", {}).get("existing_disk", {}).get("id", ""),
        "recovery_id": recovery_id,
        "ssh_identity": ssh_identity_for_instance(item),
    }


def list_vms(*, force_refresh: bool = False) -> dict[str, Any]:
    tenant_id = profile_value("tenant-id")
    cached = _read_json(INVENTORY_FILE, {})
    if cached.get("tenant_id") == tenant_id and not force_refresh:
        return {**cached, "source": "cache", "cache_age_seconds": max(0, int(time.time() - INVENTORY_FILE.stat().st_mtime))}
    personal = sync_personal_projects(tenant_id)
    result: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    def load(project: dict[str, Any]) -> list[dict[str, Any]]:
        items = _items(run_cli(
            ["compute", "instance", "list", "--parent-id", project["project_id"], "--all", "--format", "json"],
            timeout=25,
        ))
        return [_vm_summary(item, project) for item in items]

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(load, project): project for project in personal["projects"]}
        for future in as_completed(futures):
            project = futures[future]
            try:
                result.extend(future.result())
            except NebiusError as error:
                errors.append({"project_name": project["project_name"], "error": str(error)})
                # Partial outages must not turn existing VMs into an empty list.
                for vm in cached.get("vms", []) if cached.get("tenant_id") == tenant_id else []:
                    if vm.get("project_id") == project["project_id"]:
                        result.append({**vm, "stale": True})
    result.sort(key=lambda vm: (vm["state"] != "running", vm["name"].lower(), vm["id"]))
    projects = {p["project_id"]: p for p in personal["projects"]}
    for vm in _registry()["vms"]:
        if vm.get("instance_deleted") and vm.get("project_id") in projects:
            result.append({**vm, "state": "disk remains", "managed": True, "can_delete": True,
                           "project_name": projects[vm["project_id"]]["project_name"]})
    recovery = [item for item in _pending_launches() if item.get("project", {}).get("project_id") in projects]
    reusable = [item for item in _reusable_disks() if item.get("project", {}).get("project_id") in projects]
    snapshot = {
        "schema": "nebius.inventory/v1", "tenant_id": tenant_id,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "source": "live",
        "cache_age_seconds": 0, "vms": result, "errors": errors, "recovery": recovery, "reusable_disks": reusable,
        "personal_project_count": len(personal["projects"]),
        "hidden_shared_project_count": personal["hidden_shared_project_count"],
    }
    _atomic_json(INVENTORY_FILE, snapshot)
    return snapshot


def _accessible_vm(vm_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not re.fullmatch(r"computeinstance-[a-z0-9-]+", vm_id):
        raise NebiusError("Invalid VM identifier")
    instance = run_cli(["compute", "instance", "get", vm_id, "--format", "json"], timeout=30)
    project_id = instance.get("metadata", {}).get("parent_id")
    personal = sync_personal_projects()
    project = next((row for row in personal["projects"] if row["project_id"] == project_id), None)
    if not project:
        raise NebiusError("This VM is outside your personal projects in the selected tenant")
    return instance, _vm_summary(instance, project)


def live_vm_count(*, force_refresh: bool = False) -> dict[str, Any]:
    """Count exactly the running VMs visible in Your VMs; never count disks or requests."""
    result = {"count": None, "state": "unavailable", "checked_at": None,
              "scope": "Running VMs in your visible personal projects"}
    try:
        snapshot = list_vms(force_refresh=force_refresh)
        checked_at = snapshot.get("updated_at")
        checked = dt.datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
        age = max(0, (dt.datetime.now(dt.timezone.utc) - checked).total_seconds())
        result["checked_at"] = checked_at
        if not isinstance(snapshot.get("vms"), list):
            return {**result, "detail": "VM inventory is not available yet"}
        if snapshot.get("errors") or any(vm.get("stale") for vm in snapshot["vms"]):
            return {**result, "detail": "Some projects could not be checked; refresh Your VMs for details"}
        if age > 90:
            return {**result, "state": "stale", "detail": "VM count is older than 90 seconds; refreshing"}
        count = len({vm["id"] for vm in snapshot["vms"] if vm.get("id")
                     and str(vm.get("state", "")).lower() == "running" and not vm.get("instance_deleted")})
        return {**result, "state": "current", "count": count, "detail": f"{count} running VM{'s' if count != 1 else ''}"}
    except (NebiusError, ValueError, TypeError) as error:
        return {**result, "detail": str(error).splitlines()[0][:220]}


def _refresh_vm(vm_id: str) -> dict[str, Any]:
    registered = _registered(vm_id)
    instance = run_cli(["compute", "instance", "get", vm_id, "--format", "json"])
    labels = instance.get("metadata", {}).get("labels", {}) or {}
    if labels.get("managed-by") != MANAGED_BY:
        raise NebiusError("The VM no longer carries this plugin's ownership label")
    return registered


def _cloud_operation_path(resource_id):
    return STATE_DIR / "cloud-operations" / (hashlib.sha256(resource_id.encode()).hexdigest() + ".json")


def _compute_operation_id(response):
    # --async prints a bare operation ID in the pinned CLI, even with --format
    # json. Accept JSON encodings too, but never scrape an ID from error text.
    if isinstance(response, str):
        response = response.strip()
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            pass
    operation_id = response.get("id") if isinstance(response, dict) else response
    if not isinstance(operation_id, str) or not re.fullmatch(r"(?:computeoperation|operation)-[A-Za-z0-9_-]{1,128}", operation_id):
        raise NebiusError("Cloud submission returned no valid operation ID. The request may already have completed; check the resource before retrying.")
    return operation_id


def _compute_mutation(kind, action, resource_id):
    """Journal submission before waiting; interrupted requests are never replayed blindly."""
    path = _cloud_operation_path(resource_id)
    saved = _read_json(path, {})
    if saved and saved.get("action") != action:
        raise NebiusError("An earlier operation on this resource needs reconciliation in Activity")
    if saved.get("phase") == "done":
        return
    if not saved:
        saved = {"kind": kind, "action": action, "resource_id": resource_id, "phase": "submitting"}
        _atomic_json(path, saved)
        try:
            response = run_cli(["compute", kind, action, resource_id, "--async", "--format", "json"], timeout=90, parse_json=False)
        except NebiusError as error:
            if re.search(r"code = (InvalidArgument|PermissionDenied|Unauthenticated|FailedPrecondition|ResourceExhausted|NotFound)\b", str(error)):
                path.unlink(missing_ok=True)
            raise
        operation_id = _compute_operation_id(response)
        saved.update(operation_id=operation_id, phase="running")
        _atomic_json(path, saved)
    if not saved.get("operation_id"):
        # Reconcile desired state after an ambiguous submission without submitting again.
        try:
            resource = run_cli(["compute", kind, "get", resource_id, "--format", "json"])
        except NebiusError as error:
            if action == "delete" and ("notfound" in str(error).lower() or "not found" in str(error).lower()):
                _atomic_json(path, {**saved, "phase": "done"})
                return
            raise
        desired = {"start": "running", "stop": "stopped"}.get(action)
        if desired and str(resource.get("status", {}).get("state", "")).lower() == desired:
            if action == "delete":
                _atomic_json(path, {**saved, "phase": "done"})
            else:
                path.unlink(missing_ok=True)
            return
        raise NebiusError("Cloud submission outcome is unknown. Inspect the resource in the Nebius console; no duplicate request was sent.")
    operation = current_operation()
    cloud_operations = operation.get("cloud_operations", [])
    if not any(o.get("operation_id") == saved["operation_id"] for o in cloud_operations):
        cloud_operations.append({"operation_id": saved["operation_id"], "resource_id": resource_id, "action": action})
    _write_operation("running", action, operation.get("message", "Waiting for cloud operation"),
                     resource_id=resource_id, operation_id=saved["operation_id"], cloud_operations=cloud_operations)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        result = run_cli(["compute", kind, "operation", "get", saved["operation_id"], "--format", "json"])
        if result.get("status") is not None:
            status = result["status"]
            if status.get("code", 0) not in (0, "0", "OK"):
                path.unlink(missing_ok=True)
                raise NebiusError("Cloud operation failed: " + status.get("message", str(status)))
            if action == "delete":
                _atomic_json(path, {**saved, "phase": "done"})
            else:
                path.unlink(missing_ok=True)
            return
        time.sleep(3)
    raise NebiusError("Cloud operation is still pending. Resume it in Activity; its operation ID is saved.")


@cloud_mutation
def stop_vm(vm_id: str, *, automatic: bool = False) -> dict[str, Any]:
    if automatic:
        # Compatibility safety fence for any obsolete service left installed.
        return {"id": vm_id, "skipped": True, "reason": "Auto-stop has been removed; no cloud request was sent"}
    _, vm = _accessible_vm(vm_id)
    _write_operation("running", "stop", f"Stopping {vm['name']}", vm_id=vm_id)
    _compute_mutation("instance", "stop", vm_id)
    _write_operation("ready", "stop", f"{vm['name']} is stopped", vm_id=vm_id)
    return {"id": vm_id, "name": vm.get("name"), "state": "stopped"}


@cloud_mutation
def start_vm(vm_id: str) -> dict[str, Any]:
    _, vm = _accessible_vm(vm_id)
    _write_operation("running", "start", f"Starting {vm['name']}", vm_id=vm_id)
    _compute_mutation("instance", "start", vm_id)
    _write_operation("running", "boot", "VM started; waiting for its address", vm_id=vm_id, name=vm["name"])
    ready = _wait_for_instance(vm_id)
    _wait_for_vm_ssh(vm_id, vm["name"], vm.get("ssh_user"))
    _write_operation("ready", "done", f"{vm['name']} is ready for SSH", vm_id=vm_id, name=vm["name"], ssh_ready=True,
                     ssh_probe=current_operation().get("ssh_probe", {}))
    return {"id": vm_id, "name": vm.get("name"), "ssh_user": vm.get("ssh_user"), **ready,
            "ssh_ready": True, "launch_timing": current_operation()}


@cloud_mutation
def delete_saved_disk(disk_id: str, confirmed: bool = False) -> dict[str, Any]:
    if confirmed is not True:
        raise NebiusError("Disk deletion requires an explicit confirmation")
    saved = _read_json(_reusable_path(disk_id), {})
    if saved.get("state") != "available":
        raise NebiusError("This disk is not available for cleanup; refresh the overview")
    personal = sync_personal_projects()
    if saved["project"]["project_id"] not in {p["project_id"] for p in personal["projects"]}:
        raise NebiusError("This disk is outside your personal projects")
    if not _cloud_operation_path(disk_id).exists():
        disk = _verify_reusable_disk(saved)
        if disk.get("spec", {}).get("forbid_deletion"):
            raise NebiusError("This disk has deletion protection. No resources were changed")
    _write_operation("running", "delete", "Deleting unused boot disk " + saved["name"], disk_id=disk_id)
    _compute_mutation("disk", "delete", disk_id)
    _atomic_json(_reusable_path(disk_id), {**saved, "state": "deleted", "deleted_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    _cloud_operation_path(disk_id).unlink(missing_ok=True)
    _write_operation("ready", "delete", "Unused boot disk deleted", disk_id=disk_id)
    return {"disk_id": disk_id, "deleted": True}


def vm_storage(vm_id: str) -> dict[str, Any]:
    instance, vm = _accessible_vm(vm_id)
    attachments = [instance.get("spec", {}).get("boot_disk", {}), *instance.get("spec", {}).get("secondary_disks", [])]
    disks = []
    for attachment in attachments:
        disk_id = attachment.get("existing_disk", {}).get("id")
        if not disk_id:
            continue
        disk = run_cli(["compute", "disk", "get", disk_id, "--format", "json"], timeout=25)
        disks.append({"id": disk_id, "name": disk.get("metadata", {}).get("name", disk_id),
                      "size_gib": int(disk.get("status", {}).get("size_bytes") or 0) // 1024**3,
                      "state": disk.get("status", {}).get("state", "unknown"),
                      "boot": disk_id == instance.get("spec", {}).get("boot_disk", {}).get("existing_disk", {}).get("id")})
    return {"vm": vm["name"], "disks": disks,
            "note": "Attached disks remain billable when the VM stops. Delete VM and boot disk removes the reviewed boot disk; secondary disks are kept."}


def _verify_deletion_owner(instance: dict[str, Any], vm: dict[str, Any]) -> dict[str, str]:
    """Use cloud creation evidence, not a plugin label or a local install record."""
    tenant_id = profile_value("tenant-id")
    subject_id = _tenant_user_id(tenant_id)
    metadata = instance.get("metadata", {})
    if metadata.get("id") != vm["id"] or metadata.get("parent_id") != vm["project_id"]:
        raise NebiusError("The VM identity changed; refresh Your VMs before deleting")
    if instance.get("status", {}).get("managed_by") or instance.get("spec", {}).get("forbid_deletion"):
        raise NebiusError("This VM is service-managed or protected against deletion")
    try:
        created = dt.datetime.fromisoformat(metadata["created_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        raise NebiusError("Cannot verify who created this VM: its creation time is unavailable")
    audit_filter = (f"type = 'ai.nebius.compute.computeinstance.create' AND resource.metadata.id = '{vm['id']}' "
                    f"AND authentication.subject.tenant_user_id = '{subject_id}'")
    events = _items(run_cli([
        "audit", "v2", "audit-event", "list", "--parent-id", tenant_id, "--region", vm["region"],
        "--start", (created - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--end", min(created + dt.timedelta(days=1), dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--event-type", "control_plane", "--filter", audit_filter, "--all", "--format", "json",
    ], timeout=90))
    if not any(event.get("type") == "ai.nebius.compute.computeinstance.create"
               and event.get("status") == "DONE"
               and event.get("resource", {}).get("metadata", {}).get("id") == vm["id"]
               and event.get("authentication", {}).get("subject", {}).get("tenant_user_id") == subject_id
               for event in events):
        raise NebiusError("Could not verify that you created this VM. No resources were changed. "
                          "Check ownership in the Nebius console; creation audit history may be unavailable.")
    return {"tenant_id": tenant_id, "subject_id": subject_id}


@cloud_mutation
def delete_vm(vm_id: str, confirmed: bool, expected_disk_id: str | None = None) -> dict[str, Any]:
    if confirmed is not True:
        raise NebiusError("Deletion requires an explicit confirmation")
    vm = next((row for row in _registry()["vms"] if row.get("id") == vm_id), {})
    if not vm.get("deletion_owner"):
        instance, live = _accessible_vm(vm_id)
        owner = _verify_deletion_owner(instance, live)
        disk_id = instance.get("spec", {}).get("boot_disk", {}).get("existing_disk", {}).get("id", "")
        reviewed_disk = expected_disk_id if expected_disk_id is not None else vm.get("disk_id")
        if reviewed_disk is None or disk_id != reviewed_disk:
            raise NebiusError("The VM's boot disk is not the reviewed disk. Refresh its details before deleting; no resources were changed")
        if disk_id:
            disk = run_cli(["compute", "disk", "get", disk_id, "--format", "json"], timeout=25)
            if (disk.get("metadata", {}).get("id") != disk_id
                    or disk.get("metadata", {}).get("parent_id") != live["project_id"]
                    or disk.get("spec", {}).get("forbid_deletion") or disk.get("status", {}).get("managed_by")):
                raise NebiusError("The boot disk is protected or outside this VM's project; no resources were changed")
        # This is a confirmed deletion journal, not adoption or SSH ownership.
        vm = {**live, "disk_id": disk_id, "deletion_owner": owner, "deletion_only": not live.get("managed")}
        _update_vm_record(vm_id, vm)
    else:
        owner = vm["deletion_owner"]
        tenant_id = profile_value("tenant-id")
        if owner != {"tenant_id": tenant_id, "subject_id": _tenant_user_id(tenant_id)}:
            raise NebiusError("This deletion belongs to another Nebius account or tenant")
        if vm["project_id"] not in {p["project_id"] for p in sync_personal_projects()["projects"]}:
            raise NebiusError("This VM is outside your personal projects in the selected tenant")
        if expected_disk_id is not None and expected_disk_id != vm.get("disk_id"):
            raise NebiusError("The confirmed boot disk differs from the pending deletion")
    if not vm.get("instance_deleted"):
        pending = _read_json(_cloud_operation_path(vm_id), {})
        if not pending:
            instance, _ = _accessible_vm(vm_id)
            if instance.get("spec", {}).get("boot_disk", {}).get("existing_disk", {}).get("id", "") != vm.get("disk_id"):
                raise NebiusError("The VM's boot disk changed. Inspect its storage before deleting; no resources were changed")
        _write_operation("running", "delete", f"Deleting {vm.get('name')}", vm_id=vm_id)
        _compute_mutation("instance", "delete", vm_id)
        _update_vm_record(vm_id, {"instance_deleted": True})
    import nebius_ports
    nebius_ports.disable_vm(vm_id)
    disk_id = str(vm.get("disk_id") or "")
    if disk_id.startswith("computedisk-") and _cloud_operation_path(disk_id).exists():
        _compute_mutation("disk", "delete", disk_id)
    elif disk_id.startswith("computedisk-"):
        disk = run_cli(["compute", "disk", "get", disk_id, "--format", "json"], timeout=25)
        metadata, status = disk.get("metadata", {}), disk.get("status", {})
        if (metadata.get("id") != disk_id or metadata.get("parent_id") != vm["project_id"]
                or status.get("read_write_attachment") or status.get("read_only_attachments")
                or status.get("reconciling") or status.get("lock_state") or status.get("managed_by")
                or disk.get("spec", {}).get("forbid_deletion")):
            raise NebiusError("VM deleted, but its boot disk is not safe to delete yet. Refresh and retry cleanup; the disk is preserved")
        attached = _items(run_cli(["compute", "instance", "list", "--parent-id", vm["project_id"], "--all", "--format", "json"], timeout=25))
        if any(any(item.get("existing_disk", {}).get("id") == disk_id
                   for item in [row.get("spec", {}).get("boot_disk", {}), *row.get("spec", {}).get("secondary_disks", [])])
               for row in attached):
            raise NebiusError("VM deleted, but another VM uses its boot disk. The disk was preserved")
        _write_operation("running", "delete", "VM deleted; deleting its boot disk", vm_id=vm_id, disk_id=disk_id)
        _compute_mutation("disk", "delete", disk_id)
    _update_vm_record(vm_id, remove=True)
    _cloud_operation_path(vm_id).unlink(missing_ok=True)
    if disk_id:
        _cloud_operation_path(disk_id).unlink(missing_ok=True)
    return {"id": vm_id, "name": vm.get("name"), "deleted": True, "disk_deleted": bool(disk_id)}


def connect_vm(vm_id: str, *, launch: bool = True, username: str | None = None) -> dict[str, Any]:
    _, vm = _accessible_vm(vm_id)
    if vm["state"] != "running":
        raise NebiusError("Start this VM before connecting")
    address = vm.get("public_ip") or vm.get("private_ip") or ""
    if not address:
        raise NebiusError("The VM has no reachable address yet")
    try:
        ipaddress.ip_address(address)
    except ValueError as error:
        raise NebiusError("Nebius returned an invalid VM address") from error
    username = username or vm.get("ssh_user")
    if not username:
        raise NebiusError("Enter the SSH username for this VM")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
        raise NebiusError("Invalid SSH username")
    saved = _read_json(CONNECTIONS_FILE, {})
    saved[vm_id] = {"username": username}
    _atomic_json(CONNECTIONS_FILE, saved)
    command = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "HostKeyAlias=" + vm_id,
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ConnectionAttempts=2",
        "-o",
        "ServerAliveInterval=30",
    ]
    command += ssh_identity_options(vm)
    command += [f"{username}@{address}"]
    if launch:
        try:
            subprocess.Popen(
                ["omarchy-launch-tui", "--app-id=org.nebius.ssh",
                 str(Path(__file__).resolve().parent.parent / "bin/nebius-ui"), "connect", "--vm-id", vm_id, "--username", username],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            raise NebiusError(f"Could not open the SSH terminal: {error}") from error
    return {"id": vm_id, "name": vm.get("name"), "public_ip": address, "managed": bool(vm.get("managed")),
            "launched": launch, "command": command}


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capacity = sub.add_parser("capacity")
    capacity.add_argument("--refresh", action="store_true")
    projects = sub.add_parser("projects")
    projects.add_argument("--refresh", action="store_true")
    project_create = sub.add_parser("create-project")
    project_create.add_argument("--region", required=True)
    project_create.add_argument("--name")
    project_create.add_argument("--confirmed", action="store_true")
    project_create.add_argument("--dry-run", action="store_true")
    sub.add_parser("ensure-key")
    images = sub.add_parser("images")
    images.add_argument("--offering-id", action="append", required=True)
    images.add_argument("--project-id", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--name")
    plan.add_argument("--offering-id")
    plan.add_argument("--project-id")
    plan.add_argument("--image-id", default="")
    plan.add_argument("--disk-gib", type=int)
    plan.add_argument("--allocation", choices=("on_demand", "preemptible"), default="on_demand")
    plan.add_argument("--auto-stop-hours", type=int, default=0, help=argparse.SUPPRESS)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--region", required=True)
    preflight.add_argument("--allocation", choices=("on_demand", "preemptible"), default="on_demand")
    preflight.add_argument("--platform", default="")
    preflight.add_argument("--gpu-count", type=int, default=1)
    preflight.add_argument("--project-id")
    preflight.add_argument("--preset", default="")
    create = sub.add_parser("create")
    create.add_argument("--plan-id", required=True)
    create.add_argument("--dry-run", action="store_true")
    inventory = sub.add_parser("list")
    inventory.add_argument("--refresh", action="store_true")
    count = sub.add_parser("vm-count")
    count.add_argument("--refresh", action="store_true")
    recover = sub.add_parser("recover")
    recover.add_argument("--request-id", required=True)
    sub.add_parser("repair-rejected")
    archive = sub.add_parser("archive-request")
    archive.add_argument("--request-id", required=True)
    archive.add_argument("--confirmed", action="store_true")
    for command in ("start", "stop"):
        item = sub.add_parser(command)
        item.add_argument("--vm-id", required=True)
        if command == "stop":
            item.add_argument("--automatic", action="store_true", help=argparse.SUPPRESS)
    connect = sub.add_parser("connect")
    connect.add_argument("--vm-id", required=True)
    connect.add_argument("--no-launch", action="store_true")
    connect.add_argument("--username")
    delete = sub.add_parser("delete")
    delete.add_argument("--vm-id", required=True)
    delete.add_argument("--confirmed", action="store_true")
    delete.add_argument("--expected-disk-id", help="Exact boot disk shown in the deletion review; empty for no boot disk")
    disk_delete = sub.add_parser("delete-disk")
    disk_delete.add_argument("--disk-id", required=True)
    disk_delete.add_argument("--confirmed", action="store_true")
    storage = sub.add_parser("storage")
    storage.add_argument("--vm-id", required=True)
    ports = sub.add_parser("ports")
    ports.add_argument("--action", choices=["list", "add"], default="list")
    ports.add_argument("--vm-id")
    ports.add_argument("--remote-port")
    ports.add_argument("--local-port")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "capacity":
            value = gpu_capacity(force_refresh=args.refresh)
        elif args.command == "projects":
            value = sync_personal_projects(force=args.refresh)
        elif args.command == "create-project":
            value = create_nebius_project(
                args.region, name=args.name, confirmed=args.confirmed, dry_run=args.dry_run
            )
        elif args.command == "ensure-key":
            ensure_ssh_key()
            value = {"private_key": str(SSH_KEY), "public_key": str(SSH_KEY.with_suffix('.pub'))}
        elif args.command == "images":
            from nebius_catalog import list_images
            value = list_images(args.offering_id, args.project_id)
        elif args.command == "plan":
            value = plan_gpu_vm(args.name, args.offering_id, args.project_id, args.allocation,
                                args.auto_stop_hours, args.image_id, args.disk_gib)
        elif args.command == "preflight":
            value = preflight_vm(args.region, args.allocation, args.platform, args.gpu_count, project_id=args.project_id, preset=args.preset)
        elif args.command == "create":
            value = create_gpu_vm(args.plan_id, dry_run=args.dry_run)
        elif args.command == "list":
            value = list_vms(force_refresh=args.refresh)
        elif args.command == "vm-count":
            value = live_vm_count(force_refresh=args.refresh)
        elif args.command == "recover":
            value = recover_launch(args.request_id)
        elif args.command == "repair-rejected":
            value = repair_rejected_launches()
        elif args.command == "archive-request":
            value = archive_request(args.request_id, args.confirmed)
        elif args.command == "start":
            value = start_vm(args.vm_id)
        elif args.command == "stop":
            value = stop_vm(args.vm_id, automatic=args.automatic)
        elif args.command == "delete":
            value = delete_vm(args.vm_id, args.confirmed, args.expected_disk_id)
        elif args.command == "delete-disk":
            value = delete_saved_disk(args.disk_id, args.confirmed)
        elif args.command == "storage":
            value = vm_storage(args.vm_id)
        elif args.command == "ports":
            import nebius_ports
            value = nebius_ports.listing() if args.action == "list" else nebius_ports.add(args.vm_id, args.remote_port, args.local_port)
        elif args.command == "connect":
            value = connect_vm(args.vm_id, launch=not args.no_launch, username=args.username)
        else:
            raise NebiusError("Unknown command")
        _print(value)
        return 0
    except NebiusError as error:
        print(f"Nebius: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # Catalog helpers share this CLI instance, including its exception type.
    sys.modules["nebius_core"] = sys.modules[__name__]
    raise SystemExit(main())
