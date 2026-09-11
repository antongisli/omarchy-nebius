#!/usr/bin/env python3
"""Synthetic CLI for isolated worker integration tests. Never contacts Nebius."""

import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
state = Path(os.environ["NEBIUS_TEST_STATE"])
with (state / "calls.jsonl").open("a") as handle:
    handle.write(json.dumps(args) + "\n")
scenario = os.environ.get("NEBIUS_TEST_SCENARIO", "ready")


def emit(value):
    print(json.dumps(value))


if "config" in args:
    print("tenant-test" if args[-1] == "tenant-id" else "project-personal")
elif "quota-allowance" in args:
    emit({"items": [{"metadata": {"name": "compute.disk.size.network-ssd"},
                      "spec": {"region": "eu-west1", "limit": 0 if scenario == "quota-zero" else 2 * 1024**4},
                      "status": {"usage": 0, "state": "STATE_ACTIVE"}}]})
elif "whoami" in args:
    emit({"user_profile": {"tenants": [{"tenant_id": "tenant-test", "tenant_user_account_id": "tenantuseraccount-test"}]}})
elif "project" in args and "list" in args:
    emit({"items": []})
elif "project" in args and "create" in args:
    emit({"metadata": {"id": "project-created", "name": args[args.index("--name") + 1]}})
elif "subnet" in args:
    if scenario == "network-error":
        print("Network service is unavailable", file=sys.stderr)
        raise SystemExit(1)
    emit({"items": [{"metadata": {"id": "vpcsubnet-created", "name": "default"}, "status": {"state": "READY"}}]})
else:
    print("Unimplemented synthetic command: " + repr(args), file=sys.stderr)
    raise SystemExit(2)
