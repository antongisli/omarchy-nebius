"""Live VM rows, completion races and asynchronous reads using fake processes."""
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'libexec'))
import nebius_inventory as inventory
import nebius_ui as ui
from test_ui import app

VM = {'id': 'computeinstance-test', 'name': 'training', 'state': 'running', 'platform': 'gpu-h100-sxm',
      'region': 'eu-north1', 'allocation': 'on_demand', 'project_name': 'personal', 'can_delete': True}
SNAPSHOT = {'vms': [VM], 'updated_at': '2026-09-12T01:00:00+00:00'}
JOB = {'id': 'a' * 24, 'command': 'delete', 'phase': 'queued', 'started_at': '2026-09-12T02:00:00+00:00',
       'arguments': ['delete', '--vm-id', VM['id'], '--confirmed']}


class InventoryRefreshTests(unittest.TestCase):
    def test_queued_and_running_actions_replace_display_state_without_mutating_snapshot(self):
        for command, state in [('delete', 'deleting'), ('stop', 'stopping'), ('start', 'starting')]:
            for phase in ['queued', 'running']:
                row = inventory.apply_jobs(SNAPSHOT, [{**JOB, 'command': command, 'phase': phase}])['vms'][0]
                self.assertEqual(row['state'], state)
                self.assertEqual(row['operation_job_id'], JOB['id'])
                self.assertEqual(row['cloud_state'], 'running')
        self.assertEqual(SNAPSHOT['vms'][0]['state'], 'running')

    def test_finished_delete_disappears_even_if_cloud_list_is_stale(self):
        done = {**JOB, 'phase': 'ready', 'result': {'deleted': True, 'disk_deleted': True}}
        self.assertEqual(inventory.apply_jobs(SNAPSHOT, [done])['vms'], [])
        cleanup = {**SNAPSHOT, 'vms': [{**VM, 'instance_deleted': True, 'state': 'disk remains'}]}
        self.assertEqual(inventory.apply_jobs(cleanup, [done])['vms'], [])

    def test_failed_delete_does_not_hide_vm_or_show_it_as_still_deleting(self):
        failed = {**JOB, 'phase': 'error', 'finished_at': '2026-09-12T02:00:10+00:00'}
        row = inventory.apply_jobs(SNAPSHOT, [failed])['vms'][0]
        self.assertEqual(row['state'], 'running')
        self.assertIn('Delete failed', row['operation_note'])
        self.assertNotIn('operation_job_id', row)

    def test_recent_completion_bridges_only_an_older_cloud_snapshot(self):
        stopped = {**JOB, 'command': 'stop', 'phase': 'ready', 'finished_at': '2026-09-12T02:00:10+00:00',
                   'result': {'state': 'stopped'}}
        self.assertEqual(inventory.apply_jobs(SNAPSHOT, [stopped])['vms'][0]['state'], 'stopped')
        fresh = {**SNAPSHOT, 'updated_at': '2026-09-12T03:00:00+00:00'}
        self.assertEqual(inventory.apply_jobs(fresh, [stopped])['vms'][0]['state'], 'running')

    def test_latest_job_wins_and_another_vm_is_unchanged(self):
        old = {**JOB, 'command': 'start', 'phase': 'running', 'started_at': '2026-09-12T00:00:00+00:00'}
        snapshot = {**SNAPSHOT, 'vms': [VM, {**VM, 'id': 'computeinstance-other'}]}
        rows = inventory.apply_jobs(snapshot, [old, JOB])['vms']
        self.assertEqual([row['state'] for row in rows], ['deleting', 'running'])

    def test_ssh_wait_is_a_distinct_start_state(self):
        job = {**JOB, 'command': 'start', 'operation': {'stage': 'ssh'}}
        self.assertEqual(inventory.apply_jobs(SNAPSHOT, [job])['vms'][0]['state'], 'waiting for ssh')

    def test_polling_is_nonblocking_and_never_starts_overlapping_reads(self):
        process = Mock(returncode=None, poll=Mock(return_value=None))
        with patch.object(inventory.subprocess, 'Popen', return_value=process) as launch, \
             patch.object(inventory.os, 'killpg'), inventory.Poller() as poller:
            self.assertEqual(poller.poll(SNAPSHOT, [JOB]), SNAPSHOT)
            self.assertEqual(poller.poll(SNAPSHOT, [JOB], force=True), SNAPSHOT)
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[0][-2:], ['list', '--refresh'])
            process.wait.assert_not_called()

    def test_completion_during_inflight_read_discards_that_read_and_starts_a_new_one(self):
        processes = []
        def launch(*args, **kwargs):
            kwargs['stdout'].write('{"vms": [], "updated_at": "new"}')
            kwargs['stdout'].flush()
            process = Mock(returncode=None)
            process.poll.side_effect = lambda: process.returncode
            processes.append(process)
            return process
        with patch.object(inventory.subprocess, 'Popen', side_effect=launch), \
             patch.object(inventory.os, 'killpg'), inventory.Poller() as poller:
            poller.poll(SNAPSHOT, [JOB])
            processes[0].returncode = 0
            done = {**JOB, 'phase': 'ready'}
            self.assertEqual(poller.poll(SNAPSHOT, [done]), SNAPSHOT)
            self.assertEqual(len(processes), 2)
            processes[1].returncode = 0
            self.assertEqual(poller.poll(SNAPSHOT, [done])['vms'], [])

    def test_refresh_error_retains_existing_rows_and_exposes_error(self):
        process = Mock(returncode=1, poll=Mock(return_value=1))
        with inventory.Poller() as poller:
            poller.process, poller.output, poller.errors = process, io.StringIO(''), io.StringIO('Network unavailable')
            self.assertEqual(poller.poll(SNAPSHOT, []), SNAPSHOT)
            self.assertIn('Network unavailable', poller.error)

    def test_overview_changes_from_deleting_to_removed_without_keyboard_refresh(self):
        application, screen = app([None, None, '\x1b'], 80, 24)
        application.inventory = SNAPSHOT
        def entries():
            return [{**JOB, 'phase': 'ready', 'result': {'deleted': True}}] if len(screen.frames) >= 2 else [JOB]
        clock = iter(range(0, 100, 3))
        with patch.object(ui.jobs, 'jobs', side_effect=entries), \
             patch.object(inventory.Poller, 'poll', side_effect=lambda snapshot, jobs, **kwargs: snapshot), \
             patch.object(ui.time, 'monotonic', side_effect=lambda: next(clock)):
            application.overview()
        self.assertIn('DELETING', screen.frames[0])
        self.assertNotIn('training', screen.frames[-1])
        self.assertIn('No VMs found', screen.frames[-1])

    def test_active_operation_action_follows_job_instead_of_resubmitting(self):
        application, _ = app([])
        vm = inventory.apply_jobs(SNAPSHOT, [JOB])['vms'][0]
        with patch.object(application, 'watch') as watch, patch.object(application, 'mutate') as mutate:
            application.vm_actions(vm, action='delete')
        watch.assert_called_once()
        mutate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
