"""Persistent independent background jobs for terminal and agent clients."""
import datetime as dt
import fcntl
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import sys

import nebius_core as core


def _legacy_records():
    """Older workers put per-job progress in activity.log, not a sidecar file."""
    records = {}
    try:
        with (core.STATE_DIR / "activity.log").open("rb") as log:
            log.seek(0, os.SEEK_END)
            size = log.tell()
            log.seek(max(0, size - 2 * 1024 * 1024))
            if log.tell():
                log.readline()  # Discard the partial first record in a bounded tail.
            for line in log:
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    records[item["id"]] = item
    except OSError:
        pass
    return records


def describe(job):
    """Human-facing state from this job only; never borrow global progress."""
    phase, command = job.get("phase", "unknown"), job.get("command", "operation")
    operation = job.get("operation") if isinstance(job.get("operation"), dict) else {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    names = {"create": "Create VM", "create-project": "Create project", "start": "Start VM", "stop": "Stop VM",
             "delete": "Delete VM", "delete-disk": "Delete boot disk", "recover": "Recover launch",
             "repair-rejected": "Check rejected launches", "archive-request": "Archive launch request"}
    title = job.get("title")
    if not title or title == command:
        title = names.get(command, "Operation")
        resource = result.get("name") or result.get("project_name") or operation.get("name") or operation.get("project_name")
        if resource:
            title += " · " + str(resource)
    status = {"ready": "Completed", "error": "Failed", "running": "In progress", "queued": "Queued",
              "interrupted": "Check outcome"}.get(phase, "Unknown")
    if phase == "ready":
        message = operation.get("message") if operation.get("phase") in {None, "ready"} else None
        if message in {None, "", "Operation completed", "Checking your request", "Waiting for worker"}:
            message = {"create": "VM created", "create-project": "Project created", "start": "VM started",
                       "stop": "VM stopped", "delete-disk": "Boot disk deleted", "delete": "VM and boot disk deleted" if result.get("disk_deleted") else "VM deletion completed",
                       "recover": "Launch recovered", "archive-request": "Request archived locally"}.get(command, "Operation completed")
    elif phase == "error":
        raw = operation.get("message") if operation.get("phase") == "error" else job.get("error")
        message = core.explain_error(str(raw))["message"] if raw else "The operation failed. Open details to check its outcome."
    elif phase == "interrupted":
        message = "Progress tracking stopped. Check the resource's outcome before retrying."
    elif phase == "queued":
        message = "Starting this operation locally…"
    elif phase == "running":
        message = operation.get("message") or {"start": "Starting the VM…", "stop": "Stopping the VM…",
                    "delete": "Deleting the VM and checking its boot disk…", "create": "Creating the VM…"}.get(command, "Operation in progress…")
    else:
        message = "No reliable status was saved. Open details to inspect this operation."
    return {"title": title, "status": status, "message": message,
            "section": "In progress" if phase in {"running", "queued"} else "Recent activity"}


def can_resume(job):
    arguments = job.get("arguments")
    command = job.get("command")
    if job.get("phase") not in {"error", "interrupted"} or command not in {"start", "stop", "delete", "delete-disk"}:
        return False
    if not isinstance(arguments, list) or not arguments or arguments[0] != command:
        return False
    flag, prefix = ("--disk-id", "computedisk") if command == "delete-disk" else ("--vm-id", "computeinstance")
    try:
        resource = arguments[arguments.index(flag) + 1]
    except (ValueError, IndexError):
        return False
    return isinstance(resource, str) and bool(re.fullmatch(prefix + r"-[A-Za-z0-9_-]+", resource)) and (
        command not in {"delete", "delete-disk"} or "--confirmed" in arguments)


def jobs():
    result = []
    legacy = None
    for path in (core.STATE_DIR / "jobs").glob("*.json"):
        if path.name.endswith(".operation.json"):
            continue
        job = core._read_json(path, {})
        if not isinstance(job, dict) or not re.fullmatch(r"[a-f0-9]{24}", str(job.get("id", ""))):
            continue
        operation = core._read_json(path.with_name(job["id"] + ".operation.json"), {})
        if not isinstance(operation, dict) or not operation:
            if legacy is None:
                legacy = _legacy_records()
            record = legacy.get(job["id"], {})
            # Supplement missing old fields without overriding authoritative job state.
            job = {**record, **job}
            operation = job.get("operation", {})
        job["operation"] = operation if isinstance(operation, dict) else {}
        if job.get("phase") == "running":
            try:
                with (core.STATE_DIR / ("job-" + job["id"] + ".lock")).open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    job["phase"] = "interrupted"
            except BlockingIOError:
                pass  # The worker still owns this job, independent of PID reuse.
            except OSError:
                job["phase"] = "interrupted"
        if job.get("phase") == "queued":
            try:
                age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(job["started_at"])).total_seconds()
                if age > 15:
                    job["phase"] = "interrupted"
            except (KeyError, ValueError):
                job["phase"] = "interrupted"
        result.append(job)
    return sorted(result, key=lambda j: j.get("started_at", ""), reverse=True)


def submit(arguments, title=""):
    if arguments[0] in {"start", "stop", "delete", "delete-disk"}:
        flag, prefix = ("--disk-id", "computedisk") if arguments[0] == "delete-disk" else ("--vm-id", "computeinstance")
        if flag not in arguments or not re.fullmatch(prefix + r"-[A-Za-z0-9_-]+", arguments[arguments.index(flag) + 1]):
            raise core.NebiusError("Invalid resource ID")
    job_id = secrets.token_hex(12)
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    job = {"id": job_id, "phase": "queued", "command": arguments[0], "arguments": list(arguments),
           "title": title or arguments[0], "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    core._atomic_json(core.STATE_DIR / "jobs" / (job_id + ".json"), job)
    try:
        with (core.STATE_DIR / "worker.log").open("a") as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("nebius_job.py")), job_id, *arguments],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except OSError as error:
        core._atomic_json(core.STATE_DIR / "jobs" / (job_id + ".json"), {**job, "phase": "error", "error": str(error)})
        raise core.NebiusError("Could not start the operation worker: " + str(error)) from error
    return {"job_id": job_id, "phase": "submitted", "pid": process.pid}


def summary():
    entries = jobs()
    active = [j for j in entries if j["phase"] in {"queued", "running"}]
    if active:
        return {"phase": "running", "message": f"{len(active)} operation(s) running · A opens Activity", "count": len(active)}
    if entries:
        recent = entries[0]
        return {**recent.get("operation", {}), "phase": recent["phase"],
                "message": describe(recent)["message"]}
    return core._read_json(core.OPERATION_FILE, {})


if __name__ == "__main__":
    print(json.dumps(summary()))
