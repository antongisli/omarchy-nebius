"""Image discovery, hardware compatibility, and equivalent GPU variants."""
import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'libexec'))
import nebius_catalog as catalog
import nebius_core as core
from tests.test_ui import app
import nebius_ui as ui


PROJECT = {"project_id": "project-mine", "project_name": "mine", "region": "eu-north1"}
SHAPE = {"offering_id": "l40a", "tenant_id": "tenant-test", "region": "eu-north1", "platform": "gpu-l40s-a",
         "preset": "1gpu", "gpu_label": "L40S A", "gpu_count": 1, "vcpu_count": 8, "memory_gib": 32,
         "projects": [PROJECT], "on_demand": {"available": 3}, "preemptible": {"available": 0}}
IMAGE = {"metadata": {"id": "computeimage-custom", "name": "my-development-image", "parent_id": "project-shared"},
         "spec": {"cpu_architecture": "AMD64"}, "status": {"state": "READY", "min_disk_size_bytes": str(256 * 1024**3)}}


class CatalogTests(unittest.TestCase):
    def test_cli_catalog_error_is_reported_without_traceback_or_cloud_access(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'nebius'
            state.mkdir()
            shape = copy.deepcopy(SHAPE)
            shape['projects'][0]['subnet_id'] = 'vpcsubnet-test'
            (state / 'capacity.json').write_text(json.dumps({
                'schema': 'nebius.omarchy-capacity/v3', 'offerings': [shape]}))
            result = subprocess.run([sys.executable, str(Path(core.__file__)), 'plan',
                                     '--offering-id', 'l40a', '--project-id', 'project-mine',
                                     '--name', 'test', '--image-id', 'invalid'],
                                    env={**os.environ, 'HOME': directory, 'XDG_STATE_HOME': directory},
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr.strip(), 'Nebius: Invalid image ID')

    def test_equivalent_variants_group_but_sizes_regions_and_products_remain_distinct(self):
        variants = [SHAPE, {**SHAPE, "offering_id": "l40d", "platform": "gpu-l40s-d", "on_demand": {"available": 5}},
                    {**SHAPE, "offering_id": "larger", "memory_gib": 64},
                    {**SHAPE, "offering_id": "elsewhere", "region": "eu-west1"}]
        rows = catalog.configurations(variants, "on_demand")
        self.assertEqual(len(rows), 3)
        combined = next(row for row in rows if len(row['variants']) == 2)
        self.assertEqual(combined['gpu_label'], 'L40S')
        self.assertEqual(combined['on_demand']['available'], 5)  # not 3+5
        self.assertEqual(catalog.gpu_name('gpu-rtx6000'), catalog.gpu_name('gpu-rtx6000-a'))
        self.assertNotEqual(catalog.gpu_name('gpu-new-future-a'), catalog.gpu_name('gpu-new-future-d'))

    def test_variant_resolution_respects_project_image_and_allocation(self):
        other_project = {**PROJECT, "project_id": "project-other"}
        d = {**SHAPE, "offering_id": "l40d", "platform": "gpu-l40s-d", "projects": [PROJECT, other_project],
             "preemptible": {"available": 2}, "on_demand": {"available": 8}}
        group = catalog.configurations([SHAPE, d], "on_demand")[0]
        self.assertEqual(len(group['projects']), 2)
        self.assertEqual(catalog.resolve_variant(group, 'project-mine', 'preemptible')['offering_id'], 'l40d')
        self.assertEqual(catalog.resolve_variant(group, 'project-mine', 'on_demand', ['l40a'])['offering_id'], 'l40a')
        with self.assertRaises(core.NebiusError):
            catalog.resolve_variant(group, 'project-other', 'on_demand', ['l40a'])

    def test_variant_resolution_avoids_project_preemptible_restriction(self):
        blocked = {**SHAPE, "projects": [{**PROJECT, "allowed_for_preemptibles": False}],
                   "preemptible": {"available": 99}}
        allowed = {**SHAPE, "offering_id": "allowed", "platform": "gpu-l40s-d", "preemptible": {"available": 1}}
        group = catalog.configurations([blocked, allowed], "preemptible")[0]
        self.assertEqual(catalog.resolve_variant(group, "project-mine", "preemptible")["offering_id"], "allowed")

    def test_compatibility_filters_architecture_platform_preset_and_unready(self):
        for updates in ({'cpu_architecture': 'ARM64'}, {'unsupported_platforms': {'gpu-l40s-a': 'no driver'}},
                        {'unsupported_presets': [{'platform': 'gpu-l40s-a', 'preset': '1gpu', 'reason': 'too small'}]}):
            image = copy.deepcopy(IMAGE)
            image['spec'].update(updates)
            self.assertIsNone(catalog.image_row(image, [SHAPE], 'mine'))
        for updates in ({'state': 'CREATING'}, {'reconciling': True}):
            image = copy.deepcopy(IMAGE)
            image['status'].update(updates)
            self.assertIsNone(catalog.image_row(image, [SHAPE], 'mine'))
        row = catalog.image_row(IMAGE, [SHAPE], 'mine')
        self.assertEqual(row['image_id'], 'computeimage-custom')
        self.assertTrue(row['warnings'])  # Unknown driver support is visible, not excluded.

    def test_recommendation_is_not_a_support_whitelist(self):
        image = copy.deepcopy(IMAGE)
        image['spec']['recommended_platforms'] = ['gpu-h100-sxm']
        self.assertIsNotNone(catalog.image_row(image, [SHAPE], 'mine'))

    def test_unknown_cpu_architecture_is_visible(self):
        image = copy.deepcopy(IMAGE)
        image['spec'].pop('cpu_architecture')
        row = catalog.image_row(image, [SHAPE], 'mine')
        self.assertIn('CPU architecture compatibility is not specified', row['warnings'])

    def test_catalog_includes_shared_project_custom_images_and_public_with_partial_errors(self):
        projects = [{"metadata": {"id": 'project-shared', 'name': 'shared'}, 'spec': {'region': 'eu-north1'}},
                    {"metadata": {"id": 'project-denied', 'name': 'denied'}, 'spec': {'region': 'eu-north1'}},
                    {"metadata": {"id": 'project-other-region', 'name': 'other'}, 'spec': {'region': 'eu-west1'}}]
        calls = []
        def cli(args, **kwargs):
            calls.append(args)
            if args[:3] == ['iam', 'project', 'list']:
                return {'items': projects}
            if args[:3] == ['compute', 'image', 'list-public']:
                self.assertNotIn('--all', args)
                return {'items': [{**IMAGE, 'metadata': {**IMAGE['metadata'], 'id': 'computeimage-public'}}]}
            if 'project-denied' in args:
                raise core.NebiusError('permission denied')
            if 'project-shared' in args:
                return {'items': [IMAGE]}
            self.fail(args)
        with patch.object(core, 'gpu_capacity', return_value={'offerings': [SHAPE]}), \
             patch.object(core, 'profile_value', return_value='tenant-test'), patch.object(core, 'run_cli', side_effect=cli):
            result = catalog.list_images(['l40a'], 'project-mine')
        self.assertEqual({row['image_id'] for row in result['images']}, {'computeimage-custom', 'computeimage-public'})
        self.assertIn('denied', result['warnings'][0])
        self.assertFalse(any('project-other-region' in args for args in calls))

    def test_exact_image_rejects_wrong_region_before_planning(self):
        with patch.object(core, 'run_cli', side_effect=[IMAGE, {'spec': {'region': 'eu-west1'}}]):
            with self.assertRaisesRegex(core.NebiusError, 'same region'):
                catalog.get_image('computeimage-custom', SHAPE)

    def test_public_image_region_is_checked_against_catalog(self):
        image = {**IMAGE, 'metadata': {**IMAGE['metadata'], 'parent_id': 'project-e00public-images'}}
        with patch.object(core, 'run_cli', side_effect=[image, {'items': [image]}]):
            self.assertEqual(catalog.get_image('computeimage-custom', SHAPE), image)
        with patch.object(core, 'run_cli', side_effect=[image, {'items': []}]):
            with self.assertRaisesRegex(core.NebiusError, 'not available in this region'):
                catalog.get_image('computeimage-custom', SHAPE)

    def test_image_menu_can_select_custom_image_and_escape_has_no_mutations(self):
        row = catalog.image_row(IMAGE, [SHAPE], 'shared')
        application, screen = app(['j', '\n'], width=80, height=30)
        with patch.object(application, 'read', return_value={'images': [row], 'warnings': []}), \
             patch.object(application, 'mutate') as mutate:
            self.assertEqual(application.choose_image(SHAPE, 'project-mine')['image_id'], 'computeimage-custom')
            mutate.assert_not_called()
        self.assertIn('my-development-image', '\n'.join(screen.frames))
        application, _ = app(['\x1b'])
        with patch.object(application, 'read', return_value={'images': [row], 'warnings': []}), \
             patch.object(application, 'mutate') as mutate:
            with self.assertRaises(ui.Back):
                application.choose_image(SHAPE, 'project-mine')
            mutate.assert_not_called()

    def test_selected_image_reaches_launch_review_and_cancel_never_creates(self):
        row = catalog.image_row(IMAGE, [SHAPE], 'shared')
        application, _ = app([])
        application.capacity = {'projects': [PROJECT]}
        menus = iter(['image', 'review'])
        def menu(*args, **kwargs):
            try:
                return next(menus)
            except StopIteration:
                raise ui.Back()
        plan = {'preflight': {'ready': True, 'checks': [], 'warnings': []}, 'gpu_count': 1,
                'project': PROJECT, 'disk_gib': 256, 'capacity': {'available': 1},
                'boot_image': {'label': row['name'], 'note': ''}, 'image_id': row['image_id']}
        with patch.object(application, 'menu', side_effect=menu), \
             patch.object(application, 'choose_image', return_value=row), \
             patch.object(application, 'read', return_value=plan) as read, \
             patch.object(application, 'confirm_launch', return_value=False) as review, \
             patch.object(application, 'mutate') as mutate:
            with self.assertRaises(ui.Back):
                application.launch_flow(SHAPE)
        self.assertIn('computeimage-custom', read.call_args.args)
        self.assertIn('256', read.call_args.args)
        self.assertIn('my-development-image', '\n'.join(review.call_args.args[0]))
        mutate.assert_not_called()

    def test_capacity_menu_collapses_old_cached_variant_names(self):
        application, _ = app([])
        application.capacity = {'offerings': [SHAPE, {**SHAPE, 'platform': 'gpu-l40s-d', 'gpu_label': 'L40S D'}]}
        def menu(title, rows, **kwargs):
            self.assertEqual([row[0] for row in rows], ['L40S'])
            raise ui.Back()
        with patch.object(application, 'load_capacity'), patch.object(application, 'menu', side_effect=menu):
            application.capacity_flow()
