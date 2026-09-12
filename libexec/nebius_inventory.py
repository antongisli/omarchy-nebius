"""Nonblocking VM inventory refresh and local operation state for the terminal."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import nebius_core as core


def target(job):
    arguments = job.get('arguments') or []
    for flag in ('--vm-id', '--disk-id'):
        if flag in arguments and arguments.index(flag) + 1 < len(arguments):
            return arguments[arguments.index(flag) + 1]
    return (job.get('result') or {}).get('id') or job.get('operation', {}).get('vm_id')


def apply_jobs(snapshot, entries):
    """Overlay only this VM's newest job; retain actual cloud state separately."""
    latest = {}
    for job in sorted(entries, key=lambda row: row.get('started_at', ''), reverse=True):
        if target(job):
            latest.setdefault(target(job), job)
    vms = []
    for item in snapshot.get('vms', []):
        vm = dict(item)
        job = latest.get(vm['id'], {})
        command, phase = job.get('command'), job.get('phase')
        if command not in {'create', 'start', 'stop', 'delete'}:
            vms.append(vm)
            continue
        result = job.get('result') or {}
        if phase == 'ready' and command == 'delete' and result.get('deleted'):
            continue  # A completed deletion is authoritative even during list propagation.
        if phase in {'queued', 'running'}:
            vm.update(cloud_state=vm['state'], operation_job_id=job['id'], operation_phase=phase)
            vm['state'] = {'create': 'creating', 'start': 'starting', 'stop': 'stopping', 'delete': 'deleting'}[command]
            if command in {'create', 'start'} and job.get('operation', {}).get('stage') == 'ssh':
                vm['state'] = 'waiting for ssh'
        elif phase == 'ready' and job.get('finished_at', '') > snapshot.get('updated_at', ''):
            if result.get('state') in {'running', 'stopped'}:
                vm['state'] = result['state']
        elif phase in {'error', 'interrupted'} and job.get('finished_at', job.get('started_at', '')) > snapshot.get('updated_at', ''):
            vm['operation_note'] = f'{command.capitalize()} {"failed" if phase == "error" else "needs checking"} · A for details'
        vms.append(vm)
    reusable = [row for row in snapshot.get('reusable_disks', [])
                if not (latest.get(row['disk_id'], {}).get('phase') == 'ready'
                        and latest[row['disk_id']].get('command') == 'delete-disk'
                        and (latest[row['disk_id']].get('result') or {}).get('deleted'))]
    return {**snapshot, 'vms': vms, 'reusable_disks': reusable}


class Poller:
    """One cancellable read at a time; never block curses on cloud latency."""
    def __init__(self):
        self.process = None
        self.output = self.errors = None
        self.signature = None
        self.pending = False
        self.due = time.monotonic() + 30
        self.error = ''

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.process and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
            except ProcessLookupError:
                pass
        for stream in (self.output, self.errors):
            if stream:
                stream.close()
        self.process = self.output = self.errors = None

    def poll(self, snapshot, entries, *, force=False):
        now = time.monotonic()
        signature = tuple(sorted((job['id'], job.get('phase')) for job in entries))
        if (self.signature is not None and signature != self.signature) or force:
            self.pending = True
        active = any(job.get('phase') in {'queued', 'running'} for job in entries)
        if self.signature is None and active:
            self.pending = True
        self.signature = signature
        if self.process and self.process.poll() is not None:
            try:
                self.output.seek(0)
                self.errors.seek(0)
                if self.process.returncode:
                    raise ValueError(self.errors.read().strip() or 'Inventory refresh failed')
                value = json.load(self.output)
                if not isinstance(value, dict) or not isinstance(value.get('vms'), list):
                    raise ValueError('Inventory refresh returned an invalid response')
                # A completion during this read needs a newer snapshot.
                if not self.pending:
                    snapshot = value
                    self.error = ''
            except (ValueError, OSError) as failure:
                self.error = str(failure)
            finally:
                self.close()
        if self.process is None and (self.pending or now >= self.due):
            self.output, self.errors = tempfile.TemporaryFile(mode='w+'), tempfile.TemporaryFile(mode='w+')
            try:
                self.process = subprocess.Popen([sys.executable, str(Path(core.__file__)), 'list', '--refresh'],
                                                stdout=self.output, stderr=self.errors, start_new_session=True)
                self.pending = False
            except OSError as failure:
                self.error = str(failure)
                self.pending = False
                self.close()
            self.due = now + (5 if active else 30)
        return snapshot
