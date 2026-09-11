#!/usr/bin/env python3
"""Persist a terminal-launched operation even when its window closes."""

import contextlib
import datetime as dt
import fcntl
import io
import json
import os
from pathlib import Path
import re
import sys

import nebius_core as core


def main():
    job_id, *arguments = sys.argv[1:]
    if not re.fullmatch(r"[a-f0-9]{24}", job_id) or not arguments or arguments[0] not in {
        "create", "create-project", "start", "stop", "delete", "delete-disk", "recover", "archive-request", "repair-rejected"
    }:
        return 2
    job_path = core.STATE_DIR / "jobs" / f"{job_id}.json"
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (core.STATE_DIR / "ui-job.lock").open("a") as lock, contextlib.ExitStack() as ownership:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            core._atomic_json(job_path, {"phase": "error", "error": "Another operation is running. Open activity."})
            return 1
        try:
            ownership.enter_context(core.mutation_guard())
        except core.NebiusError as error:
            core._atomic_json(job_path, {"phase": "error", "error": str(error)})
            return 1
        job = {
            "id": job_id, "pid": os.getpid(), "phase": "running", "command": arguments[0],
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        core._atomic_json(job_path, job)
        core._atomic_json(core.STATE_DIR / "active-job.json", job)
        core._write_operation("running", "checking", "Checking your request")
        output, errors = io.StringIO(), io.StringIO()
        sys.argv = [str(Path(core.__file__)), *arguments]
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = core.main()
        except Exception as error:
            code = 1
            errors.write(str(error))
        job["phase"] = "ready" if code == 0 else "error"
        job["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        operation = core._read_json(core.OPERATION_FILE, {})
        if code == 0:
            job["result"] = json.loads(output.getvalue() or "{}")
            if operation.get("phase") == "running":
                core._write_operation("ready", "done", "Operation completed", **{
                    key: value for key, value in operation.items()
                    if key not in {"phase", "stage", "message", "updated_at", "schema"}
                })
        else:
            job["error"] = errors.getvalue().strip() or "Operation stopped unexpectedly"
            explanation = core.explain_error(job["error"])
            details = {key: value for key, value in operation.items()
                       if key not in {"phase", "stage", "message", "updated_at", "schema", "details", "recovery"}}
            recovery = operation.get("recovery") or explanation["recovery"]
            if operation.get("project_id") and not operation.get("vm_id"):
                recovery = f"Project {operation.get('project_name') or operation['project_id']} already exists. " + recovery
            core._write_operation("error", operation.get("stage", "request"), explanation["message"],
                                  details=job["error"], recovery=recovery, **details)
        core._atomic_json(job_path, job)
        core._atomic_json(core.STATE_DIR / "active-job.json", job)
        with (core.STATE_DIR / "activity.log").open("a", encoding="utf-8") as log:
            log.write(json.dumps({**job, "operation": core._read_json(core.OPERATION_FILE, {})}) + "\n")
        return code


if __name__ == "__main__":
    raise SystemExit(main())
