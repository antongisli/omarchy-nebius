"""SSH completion and durable launch timing regressions; no real SSH or cloud."""
import datetime as dt
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'libexec'))
import nebius_core as core
import nebius_jobs as jobs
import nebius_ssh as ssh
import nebius_timing as timing
import nebius_ui as ui
from test_ui import app

VM = {'id': 'computeinstance-test', 'name': 'training', 'ssh_user': 'dev', 'state': 'running'}


def report():
    value = {}
    for phase, stage, elapsed in [('running', 'checking', 0), ('running', 'disk', 4),
                                  ('running', 'boot', 10), ('running', 'ssh', 19), ('ready', 'done', 23)]:
        now = (dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=elapsed)).isoformat()
        value = {**timing.advance(value, phase, stage, now), 'phase': phase, 'stage': stage}
    return {**value, 'ssh_ready': True, 'vm_id': VM['id'], 'name': VM['name'], 'message': 'SSH login verified'}


class LaunchReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = Path(temporary.name)
        for key, path in {'STATE_DIR': state, 'REGISTRY_FILE': state / 'vms.json', 'OPERATION_FILE': state / 'operation.json'}.items():
            context = patch.object(core, key, path)
            context.start()
            self.addCleanup(context.stop)

    def test_stage_durations_sum_to_total_and_freeze_after_completion(self):
        value = report()
        self.assertEqual(value['elapsed_seconds'], 23)
        self.assertEqual([row['elapsed_seconds'] for row in value['timeline']], [4, 6, 9, 4])
        self.assertEqual(timing.headline(value), 'Total to SSH ready: 23.0s')
        self.assertEqual(timing.elapsed(value), 23)

    def test_progress_has_one_total_from_the_job_not_time_since_opening_screen(self):
        operation = {**report(), 'phase': 'running', 'ssh_ready': False}
        for width, height in [(48, 20), (80, 24)]:
            application, screen = app([], width, height)
            with patch.object(core, '_read_json', return_value=operation), \
                 patch.object(timing, 'elapsed', return_value=65), patch.object(ui.time, 'time', return_value=103):
                application.progress('Creating VM', 100, mutation=True)
            frame = screen.frames[-1]
            self.assertEqual(frame.count('Total elapsed:'), 1)
            self.assertIn('Total elapsed: 1m 05.0s', frame)
            self.assertNotIn('0:03 elapsed', frame)

    def test_repeated_stage_updates_do_not_reset_its_start(self):
        first = timing.advance({}, 'running', 'ssh', '2026-09-12T00:00:00+00:00')
        second = timing.advance(first, 'running', 'ssh', '2026-09-12T00:00:02+00:00')
        final = timing.advance(second, 'error', 'ssh', '2026-09-12T00:00:05+00:00')
        self.assertEqual(len(final['timeline']), 1)
        self.assertEqual(final['timeline'][0]['elapsed_seconds'], 5)
        self.assertEqual(final['timeline'][0]['outcome'], 'error')

    def test_progress_is_saved_per_job_and_another_job_cannot_replace_it(self):
        with patch.dict(core.os.environ, {'NEBIUS_JOB_ID': 'a' * 24}):
            core._write_operation('running', 'disk', 'Boot disk')
            core._write_operation('running', 'ssh', 'Waiting for SSH')
            first = core.current_operation()
        with patch.dict(core.os.environ, {'NEBIUS_JOB_ID': 'b' * 24}):
            core._write_operation('running', 'stop', 'Stop another VM')
            self.assertEqual(len(core.current_operation()['timeline']), 1)
        with patch.dict(core.os.environ, {'NEBIUS_JOB_ID': 'a' * 24}):
            self.assertEqual(core.current_operation(), first)

    def test_start_is_not_complete_until_authenticated_ssh_succeeds(self):
        def probe(connection, **kwargs):
            self.assertEqual(core.current_operation()['stage'], 'ssh')
            self.assertEqual(core.current_operation()['phase'], 'running')
            kwargs['progress']('Waiting for SSH key', 2)
            kwargs['record']({'attempt': 1, 'elapsed_seconds': 2, 'outcome': 'ready'})
            return True
        with patch.object(core, '_accessible_vm', return_value=({}, VM)), \
             patch.object(core, '_compute_mutation'), patch.object(core, '_wait_for_instance', return_value=VM), \
             patch.object(core, 'connect_vm', return_value={'command': ['ssh', 'dev@example']}), \
             patch.object(ssh, 'wait_ready', side_effect=probe):
            result = core.start_vm(VM['id'])
        self.assertTrue(result['ssh_ready'])
        self.assertEqual(result['launch_timing']['ssh_probe']['attempts'][0]['outcome'], 'ready')
        self.assertIn('preparation_seconds', result['launch_timing']['ssh_probe'])
        self.assertEqual(core.current_operation()['phase'], 'ready')
        self.assertEqual([row['stage'] for row in result['launch_timing']['timeline']], ['start', 'boot', 'ssh'])

    def test_readiness_failure_preserves_vm_and_failed_ssh_stage(self):
        with patch.object(core, 'connect_vm', return_value={'command': ['ssh']}), \
             patch.object(ssh, 'wait_ready', side_effect=ssh.SSHError('Connection refused')), \
             patch.object(core, 'run_cli') as cloud:
            with self.assertRaisesRegex(core.NebiusError, 'SSH login is not ready'):
                core._wait_for_vm_ssh(VM['id'], VM['name'], 'dev')
        self.assertEqual(core.current_operation()['timeline'][-1]['outcome'], 'error')
        self.assertIn('Do not create another VM', core.current_operation()['recovery'])
        self.assertNotIn('ssh_ready', core.current_operation())
        cloud.assert_not_called()

    def test_unverified_login_never_opens_an_interactive_session(self):
        application, _ = app([])
        with patch.object(jobs, 'latest_vm_job', return_value=None), \
             patch.object(application, 'read', return_value={'command': ['ssh'], 'name': 'training'}), \
             patch.object(application, 'menu', return_value='overview'), \
             patch.object(ssh, 'wait_ready', return_value=False), patch.object(ui.subprocess, 'run') as session:
            application.ssh(dict(VM))
        session.assert_not_called()
        self.assertIn('no session was opened', core._read_json(core.STATE_DIR / 'ssh-last-error.json', {})['error'])

    def test_connect_during_launch_follows_the_existing_worker(self):
        launch = {'id': 'a' * 24, 'phase': 'running'}
        application, _ = app([])
        with patch.object(jobs, 'latest_vm_job', side_effect=[launch, {**launch, 'phase': 'ready'}]), \
             patch.object(application, 'watch') as watch, patch.object(application, 'operation_result') as result, \
             patch.object(ssh, 'wait_ready') as probe:
            application.ssh(dict(VM))
        watch.assert_called_once()
        result.assert_called_once()
        probe.assert_not_called()

    def test_completed_report_remains_after_an_ssh_attempt_at_each_terminal_size(self):
        for width, height in [(48, 20), (80, 24), (120, 44)]:
            application, screen = app(['c', '\x1b'], width, height)
            with patch.object(application, 'open_ssh', side_effect=ui.Back) as session:
                application.operation_result({'operation': report(), 'result': VM})
            session.assert_called_once()
            self.assertEqual(len(screen.frames), 2)
            for frame in screen.frames:
                self.assertIn('Total to SSH ready: 23.0s', frame)
                self.assertIn('SSH ready: 4.0s', frame)
                self.assertIn('Boot disk: 6.0s', frame)

    def test_failed_launch_with_existing_vm_keeps_report_instead_of_returning_to_creation(self):
        job_id = 'a' * 24
        job = {'id': job_id, 'command': 'create', 'phase': 'error', 'started_at': '2026-09-12T00:00:00+00:00'}
        operation = {**report(), 'ssh_ready': False, 'phase': 'error', 'message': 'SSH did not become ready'}
        core._atomic_json(core.STATE_DIR / 'jobs' / (job_id + '.json'), job)
        core._atomic_json(core.STATE_DIR / 'jobs' / (job_id + '.operation.json'), operation)
        application, _ = app([])
        with patch.object(application, 'operation_result') as result, self.assertRaises(ui.Background):
            application.watch(job_id, 'Creating VM')
        self.assertEqual(result.call_args.args[0]['operation'], operation)

    def test_reading_timings_after_ssh_failure_does_not_retry_connection(self):
        application, _ = app([])
        with patch.object(jobs, 'latest_vm_job', return_value={'operation': report()}), \
             patch.object(application, 'read', return_value={'command': ['ssh'], 'name': 'training'}), \
             patch.object(application, 'menu', side_effect=['timings', 'overview']), \
             patch.object(application, 'message') as message, \
             patch.object(ssh, 'wait_ready', side_effect=ssh.SSHError('Timed out')) as probe:
            application.ssh(dict(VM))
        probe.assert_called_once()
        self.assertIn('Total to SSH ready: 23.0s', message.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
