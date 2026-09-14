"""User ownership, reviewed storage and resumable deletion, using fake cloud data."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'libexec'))
import nebius_core as core
import nebius_ports as ports


class DeletionOwnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = Path(temporary.name)
        for key, value in {'STATE_DIR': state, 'REGISTRY_FILE': state / 'vms.json',
                           'CONNECTIONS_FILE': state / 'connections.json', 'OPERATION_FILE': state / 'operation.json'}.items():
            self.mock(core, key, value)
        self.project = {'project_id': 'project-mine', 'project_name': 'Mine', 'region': 'eu-north1'}
        self.instance = {'metadata': {'id': 'computeinstance-mine', 'parent_id': 'project-mine',
                                      'name': 'existing', 'created_at': '2026-09-11T17:21:00Z'},
                         'spec': {'boot_disk': {'existing_disk': {'id': 'computedisk-boot'}},
                                  'secondary_disks': [{'existing_disk': {'id': 'computedisk-data'}}]},
                         'status': {'state': 'STOPPED'}}
        self.disk = {'metadata': {'id': 'computedisk-boot', 'parent_id': 'project-mine'}, 'status': {}, 'spec': {}}
        self.event = {'type': 'ai.nebius.compute.computeinstance.create', 'status': 'DONE',
                      'authentication': {'subject': {'tenant_user_id': 'tenantuseraccount-me'}},
                      'resource': {'metadata': {'id': 'computeinstance-mine'}}}
        self.mock(core, 'profile_value', return_value='tenant-mine')
        self.identity = self.mock(core, '_tenant_user_id', return_value='tenantuseraccount-me')
        self.projects = self.mock(core, 'sync_personal_projects', return_value={'projects': [self.project]})
        self.cli = self.mock(core, 'run_cli', side_effect=self.read)
        self.mutate = self.mock(core, '_compute_mutation')
        self.remove_ports = self.mock(ports, 'remove_vm', return_value=2)

    def mock(self, obj, key, *args, **kwargs):
        context = patch.object(obj, key, *args, **kwargs)
        result = context.start()
        self.addCleanup(context.stop)
        return result

    def read(self, command, **kwargs):
        if command[:3] == ['compute', 'instance', 'get']:
            return copy.deepcopy(self.instance)
        if command[:3] == ['compute', 'disk', 'get']:
            return copy.deepcopy(self.disk)
        if command[:3] == ['compute', 'instance', 'list']:
            return {'items': []}
        if command[:4] == ['audit', 'v2', 'audit-event', 'list']:
            return {'items': [copy.deepcopy(self.event)]}
        raise AssertionError(command)

    def delete(self, disk='computedisk-boot'):
        return core.delete_vm('computeinstance-mine', True, disk)

    def test_own_vm_without_any_plugin_labels_or_registration_is_deletable(self):
        summary = core._vm_summary(self.instance, self.project)
        self.assertTrue(summary['can_delete'])
        self.assertFalse(summary['managed'])
        self.assertEqual(summary['disk_id'], 'computedisk-boot')
        result = self.delete()
        self.assertTrue(result['disk_deleted'])
        self.assertEqual(result['ports_removed'], 2)
        self.assertEqual([call.args for call in self.mutate.call_args_list],
                         [('instance', 'delete', 'computeinstance-mine'), ('disk', 'delete', 'computedisk-boot')])
        self.remove_ports.assert_called_once_with('computeinstance-mine')
        self.assertFalse(core._registry()['vms'])

    def test_other_users_vm_is_rejected_even_with_plugin_labels(self):
        self.instance['metadata']['labels'] = {'managed-by': core.MANAGED_BY}
        self.event['authentication']['subject']['tenant_user_id'] = 'tenantuseraccount-other'
        with self.assertRaisesRegex(core.NebiusError, 'verify that you created'):
            self.delete()
        self.mutate.assert_not_called()
        self.assertFalse(core._registry()['vms'])

    def test_wrong_resource_in_audit_is_rejected(self):
        self.event['resource']['metadata']['id'] = 'computeinstance-other'
        with self.assertRaises(core.NebiusError):
            self.delete()
        self.mutate.assert_not_called()

    def test_unfinished_creation_event_is_not_ownership_evidence(self):
        self.event['status'] = 'STARTED'
        with self.assertRaises(core.NebiusError):
            self.delete()
        self.mutate.assert_not_called()

    def test_outside_personal_project_is_rejected(self):
        self.projects.return_value = {'projects': []}
        with self.assertRaisesRegex(core.NebiusError, 'outside your personal projects'):
            self.delete()
        self.mutate.assert_not_called()

    def test_changed_or_unreviewed_boot_disk_blocks_all_mutations(self):
        for reviewed in ('computedisk-old', None):
            with self.subTest(reviewed=reviewed), self.assertRaisesRegex(core.NebiusError, 'reviewed disk'):
                self.delete(reviewed)
        self.mutate.assert_not_called()

    def test_protected_boot_disk_blocks_instance_deletion(self):
        self.disk['spec']['forbid_deletion'] = True
        with self.assertRaisesRegex(core.NebiusError, 'protected'):
            self.delete()
        self.mutate.assert_not_called()

    def test_service_managed_instance_is_rejected(self):
        self.instance['status']['managed_by'] = 'nodegroup'
        self.assertFalse(core._vm_summary(self.instance, self.project)['can_delete'])
        with self.assertRaisesRegex(core.NebiusError, 'service-managed'):
            self.delete()
        self.mutate.assert_not_called()

    def test_attached_disk_cleanup_can_resume_without_a_second_vm_deletion(self):
        self.disk['status']['read_write_attachment'] = 'computeinstance-other'
        with self.assertRaisesRegex(core.NebiusError, 'not safe to delete'):
            self.delete()
        saved = core._registered('computeinstance-mine')
        self.assertTrue(saved['instance_deleted'])
        self.assertTrue(saved['deletion_only'])
        self.disk['status'] = {}
        self.assertTrue(self.delete()['disk_deleted'])
        self.assertEqual([call.args for call in self.mutate.call_args_list],
                         [('instance', 'delete', 'computeinstance-mine'), ('disk', 'delete', 'computedisk-boot')])

    def test_resume_cannot_use_a_different_account(self):
        self.disk['status']['read_write_attachment'] = 'computeinstance-other'
        with self.assertRaises(core.NebiusError):
            self.delete()
        self.identity.return_value = 'tenantuseraccount-other'
        self.mutate.reset_mock()
        with self.assertRaisesRegex(core.NebiusError, 'another Nebius account'):
            self.delete()
        self.mutate.assert_not_called()

    def test_vm_without_boot_disk_can_be_deleted(self):
        self.instance['spec']['boot_disk'] = {}
        self.assertFalse(self.delete('')['disk_deleted'])
        self.mutate.assert_called_once_with('instance', 'delete', 'computeinstance-mine')


if __name__ == '__main__':
    unittest.main()
