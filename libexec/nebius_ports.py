"""Loopback-only SSH forwards, restored by a systemd user service after login."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import tempfile

import nebius_core as core

UNIT = "nebius-ports.service"


def mappings():
    return core._read_json(core.STATE_DIR / "ports.json", [])


def port(value):
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 65535:
        raise core.NebiusError("Choose a port from 1 to 65535")
    return int(value)


def available(value):
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", value))
        return True
    except OSError:
        return False


def validate_local_port(value):
    value = port(value)
    if value < 1024:
        raise core.NebiusError("Choose a local port from 1024 to 65535; the remote port can still be below 1024.")
    if any(m["local_port"] == value for m in mappings()) or not available(value):
        raise core.NebiusError(f"Local port {value} is in use. Enter another port, such as {value + 1 if value < 65535 else 18000}.")
    return value


def unit_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user" / UNIT


def install():
    if not shutil.which("systemctl"):
        raise core.NebiusError("Persistent forwarding requires systemd on your Omarchy desktop")
    def quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Unit]\nDescription=Nebius local ports\n\n[Service]\nType=simple\n"
                    + "ExecStart=" + quote(sys.executable) + " " + quote(Path(__file__).resolve()) + " serve\n"
                    + "Environment=" + quote("XDG_STATE_HOME=" + str(core.STATE_DIR.parent)) + "\n"
                    + "Restart=always\nRestartSec=5\nKillMode=control-group\n\n[Install]\nWantedBy=default.target\n")
    for args in (["daemon-reload"], ["enable", "--now", UNIT]):
        result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise core.NebiusError("Could not enable local ports: " + result.stderr.strip())


def add(vm_id, remote_port, local_port=None, username=None):
    remote = port(remote_port)
    local = port(local_port if local_port is not None else remote if remote >= 1024 else remote + 8000)
    _, vm = core._accessible_vm(vm_id)
    user = username or vm.get("ssh_user") or (core.SSH_USER if vm.get("managed") else "")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise core.NebiusError("Set this VM's SSH username in Connection settings first")
    with core.mutation_guard(wait=True, resource="ports"):
        saved = mappings()
        validate_local_port(local)
        install()
        item = {"id": secrets.token_hex(8), "vm_id": vm_id, "vm_name": vm["name"], "username": user,
                "managed": vm.get("managed", False), "ssh_identity": vm.get("ssh_identity", "default"),
                "address": vm.get("public_ip") or vm.get("private_ip"),
                "local_port": local, "remote_port": remote, "enabled": True}
        core._atomic_json(core.STATE_DIR / "ports.json", saved + [item])
    return item


def change(mapping_id, action):
    if action not in {"pause", "resume", "remove"}:
        raise core.NebiusError("Unknown port action")
    with core.mutation_guard(wait=True, resource="ports"):
        saved = mappings()
        item = next((m for m in saved if m["id"] == mapping_id), None)
        if item is None:
            raise core.NebiusError("This saved port no longer exists")
        if action == "remove":
            saved.remove(item)
        else:
            if action == "resume":
                if item.get("deleted"):
                    raise core.NebiusError("This VM was deleted. Remove this mapping and choose another VM.")
                install()
            item["enabled"] = action == "resume"
        core._atomic_json(core.STATE_DIR / "ports.json", saved)
    return {"id": mapping_id, "action": action}


def disable_vm(vm_id):
    with core.mutation_guard(wait=True, resource="ports"):
        saved = mappings()
        for item in saved:
            if item["vm_id"] == vm_id:
                item.update(enabled=False, deleted=True)
        if saved:
            core._atomic_json(core.STATE_DIR / "ports.json", saved)


def listing():
    status = core._read_json(core.STATE_DIR / "ports-status.json", {})
    fresh = time.time() - status.get("updated_at", 0) < 45
    result = []
    for item in mappings():
        state = status.get("ports", {}).get(item["id"], {}) if fresh else {}
        label = "VM deleted" if item.get("deleted") else "Paused" if not item["enabled"] else state.get("state", "Waiting for local service")
        result.append({**item, **state, "state": label, "url": f"http://127.0.0.1:{item['local_port']}"})
    return result


def ssh_command(item, address, control):
    ipaddress.ip_address(address)
    command = ["ssh", "-N", "-T", "-n", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
               "-o", "HostKeyAlias=" + item["vm_id"], "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3", "-o", "ExitOnForwardFailure=yes", "-o", "ControlMaster=yes",
               "-o", "ControlPersist=no", "-S", str(control),
               "-L", f"127.0.0.1:{item['local_port']}:127.0.0.1:{item['remote_port']}"]
    command += core.ssh_identity_options(item)
    return command + [item["username"] + "@" + address]


def serve():
    # systemd owns this process and every SSH child; no detached SSH processes.
    import concurrent.futures
    root = core.STATE_DIR / "ports-runtime"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    sockets = Path(tempfile.mkdtemp(prefix="nebius-ports-"))
    children, statuses, cache, futures = {}, {}, {}, {}
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    def resolve(vm_id):
        return core.run_cli(["compute", "instance", "get", vm_id, "--format", "json"], timeout=20)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    def terminate(key):
        child = children.pop(key, None)
        if child:
            child[0].terminate()
            try:
                child[0].wait(timeout=3)
            except subprocess.TimeoutExpired:
                child[0].kill()
                child[0].wait()
            child[1].close()
    try:
        while not stopping:
            saved = mappings()
            enabled = {m["id"]: m for m in saved if m["enabled"] and not m.get("deleted")}
            for key in list(children):
                if key not in enabled:
                    terminate(key)
            for vm_id, future in list(futures.items()):
                if future.done():
                    try:
                        resource = future.result()
                        state = str(resource.get("status", {}).get("state", "")).lower()
                        interfaces = resource.get("status", {}).get("network_interfaces", [])
                        address = next((str(n.get("public_ip_address", {}).get("address") or n.get("ip_address", {}).get("address") or "").split("/")[0] for n in interfaces), "")
                        cache[vm_id] = {"state": state, "address": address, "checked": time.monotonic(),
                                        "ssh_identity": core.ssh_identity_for_instance(resource)}
                    except Exception as error:
                        cache[vm_id] = {**cache.get(vm_id, {}), "checked": time.monotonic(), "error": str(error)}
                        if "notfound" in str(error).lower() or "not found" in str(error).lower():
                            cache[vm_id]["state"] = "deleted"
                            disable_vm(vm_id)
                    del futures[vm_id]
            for key, item in enabled.items():
                vm_id = item["vm_id"]
                info = cache.get(vm_id, {})
                if vm_id not in futures and time.monotonic() - info.get("checked", -60) >= 60:
                    futures[vm_id] = pool.submit(resolve, vm_id)
                address = info.get("address") or item.get("address")
                if info.get("state") and info["state"] != "running":
                    terminate(key)
                    statuses[key] = {"state": "VM " + info["state"]}
                    continue
                child = children.get(key)
                if child and child[2] != address:
                    terminate(key)
                    child = None
                if child and child[0].poll() is not None:
                    child[1].close()
                    children.pop(key)
                    detail = (root / (key + ".log")).read_text(errors="replace")[-1000:]
                    statuses[key] = {"state": "Reconnecting", "detail": detail, "retry_at": time.time() + 10}
                    child = None
                if not child and time.time() >= statuses.get(key, {}).get("retry_at", 0):
                    if not address:
                        statuses[key] = {"state": "Waiting for VM address", "detail": info.get("error", "")}
                        continue
                    if not available(item["local_port"]):
                        statuses[key] = {"state": "Local port in use", "detail": "Free the port or remove this mapping and choose another."}
                        continue
                    control = sockets / (key + ".sock")
                    control.unlink(missing_ok=True)
                    log = (root / (key + ".log")).open("w")
                    try:
                        connection = {**item, "ssh_identity": info.get("ssh_identity", item.get("ssh_identity", "default"))}
                        process = subprocess.Popen(ssh_command(connection, address, control), stdin=subprocess.DEVNULL,
                                                   stdout=subprocess.DEVNULL, stderr=log)
                    except (OSError, ValueError) as error:
                        log.close()
                        statuses[key] = {"state": "Connection error", "detail": str(error), "retry_at": time.time() + 10}
                        continue
                    child = children[key] = (process, log, address)
                if child:
                    control = sockets / (key + ".sock")
                    check = subprocess.run(["ssh", "-S", str(control), "-O", "check", item["username"] + "@" + address],
                                           capture_output=True, timeout=3)
                    statuses[key] = {"state": "Connected" if check.returncode == 0 else "Connecting",
                                     "detail": "Tunnel status only; start the application on the VM to use this port."}
            core._atomic_json(core.STATE_DIR / "ports-status.json", {"updated_at": time.time(), "ports": statuses})
            time.sleep(2)
    finally:
        for key in list(children):
            terminate(key)
        pool.shutdown(wait=False, cancel_futures=True)
        shutil.rmtree(sockets)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    sub.add_parser("serve")
    add_parser = sub.add_parser("add")
    add_parser.add_argument("--vm-id", required=True)
    add_parser.add_argument("--remote-port", required=True)
    add_parser.add_argument("--local-port")
    add_parser.add_argument("--username")
    for action in ("pause", "resume", "remove"):
        sub.add_parser(action).add_argument("id")
    args = parser.parse_args()
    try:
        if args.action == "serve":
            serve()
            return
        value = listing() if args.action == "list" else add(args.vm_id, args.remote_port, args.local_port, args.username) if args.action == "add" else change(args.id, args.action)
        print(json.dumps(value))
    except core.NebiusError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
