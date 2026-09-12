"""Bounded, cancellable SSH login readiness checks; no VM mutations."""
import subprocess
import tempfile
import time


class SSHCancelled(Exception):
    pass


class SSHError(Exception):
    pass


def probe_command(connection, *, connect_timeout=5):
    # OpenSSH uses the first value for each option. Override short probe limits
    # while retaining the exact identity, host-key alias and verification policy.
    return [connection["command"][0], "-T", "-n", "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={connect_timeout}", "-o", "ConnectionAttempts=1",
            *connection["command"][1:], "true"]


def wait_ready(connection, *, progress=lambda message, elapsed: None, cancelled=lambda: False, timeout=120,
               record=lambda attempt: None):
    """Authenticate, not just open TCP 22. Retry boot races, never changed keys."""
    started = time.monotonic()
    attempt_number = 0
    last_error = "The VM is running, but its SSH login service has not answered yet."
    while time.monotonic() - started < timeout:
        progress(last_error, int(time.monotonic() - started))
        if cancelled():
            raise SSHCancelled()
        with tempfile.TemporaryFile(mode="w+") as errors:
            attempt_number += 1
            attempt = time.monotonic()
            # Catch a newly opened listener promptly. Restore patient handshakes
            # after the initial boot window for slower networks or loaded hosts.
            connect_timeout = 2 if attempt - started < 30 else 5
            try:
                process = subprocess.Popen(probe_command(connection, connect_timeout=connect_timeout), stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=errors)
            except OSError as error:
                raise SSHError("Could not start SSH: " + str(error)) from error
            try:
                while process.poll() is None:
                    progress(last_error, int(time.monotonic() - started))
                    if cancelled():
                        raise SSHCancelled()
                    if time.monotonic() - attempt >= 12 or time.monotonic() - started >= timeout:
                        break
                    time.sleep(0.1)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            errors.seek(0)
            detail = errors.read()[-4000:].strip()
        elapsed = time.monotonic() - started
        record({"attempt": attempt_number, "elapsed_seconds": round(elapsed, 3),
                "duration_seconds": round(time.monotonic() - attempt, 3),
                "connect_timeout_seconds": connect_timeout,
                "outcome": "ready" if process.returncode == 0 else "not_ready",
                "detail": detail.splitlines()[-1][:300] if detail else ""})
        if process.returncode == 0:
            return True
        lower = detail.lower()
        if any(text in lower for text in ("host key verification failed", "identification has changed", "offending", "bad configuration")):
            raise SSHError(detail)
        denied = "permission denied" in lower
        retryable = denied or not detail or any(text in lower for text in (
            "connection refused", "timed out", "connection reset", "connection closed",
            "no route to host", "network is unreachable", "kex_exchange_identification"))
        if not retryable:
            raise SSHError(detail)
        last_error = ("Waiting for the VM's login user and SSH key to become ready." if denied
                      else "Waiting for the VM's SSH service to become reachable.")
        if detail:
            last_error += "\n" + detail.splitlines()[-1]
        retry_at = time.monotonic() + (0.5 if elapsed < 30 else 2)
        while time.monotonic() < retry_at and time.monotonic() - started < timeout:
            progress(last_error, int(time.monotonic() - started))
            if cancelled():
                raise SSHCancelled()
            time.sleep(0.1)
    raise SSHError(f"SSH did not become ready within {timeout} seconds.\n{last_error}\n"
                   "The VM is unchanged. Retry, or check its SSH username, key and network rules.")
