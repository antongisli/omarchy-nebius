"""Global cap behavior across synthetic projects, platforms and VM reviews."""

import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tests import test_pricing as fixture

core = fixture.core
pricing = fixture.pricing
global_pricing = fixture.global_pricing
PROJECT, OFFERING = fixture.PROJECT, fixture.OFFERING


class GlobalPricingTests(unittest.TestCase):
    setUp = fixture.PricingTests.setUp
    cli = fixture.PricingTests.cli
    writes = fixture.PricingTests.writes
    plan = fixture.PricingTests.plan

    def select(self, maximum="3.125"):
        saved = global_pricing.create_policy("Everywhere", maximum)
        global_pricing.select_default(saved["id"])
        return saved

    def materialize(self, project_id=None, platform=None):
        project_id, platform = project_id or PROJECT["project_id"], platform or OFFERING["platform"]
        selected = global_pricing.resolve(project_id, platform)
        return pricing.refresh_terms(selected, project_id, platform, create=True)

    def stopped_vm(self, policy_id=""):
        vm = {"id": "computeinstance-example", "project_id": PROJECT["project_id"],
              "platform": OFFERING["platform"], "state": "stopped", "preset": OFFERING["preset"], "name": "example"}
        spec = {"preemptible": {"on_preemption": "STOP"}, "stopped": True}
        spec.update({"spot_pricing_policy": {"id": policy_id}} if policy_id else {"follows_spot_price": {}})
        return {"metadata": {"id": vm["id"], "parent_id": vm["project_id"], "resource_version": "7"}, "spec": spec}, vm

    def test_global_settings_create_edit_and_default_work_offline(self):
        with patch.object(core, "run_cli", side_effect=AssertionError("No cloud call allowed")), \
             patch.object(core, "sync_personal_projects", side_effect=AssertionError("No project needed")):
            saved = self.select()
            self.assertEqual(global_pricing.create_policy("Everywhere", "3.125"), saved)
            edited = global_pricing.update_policy(saved["id"], "All GPUs", "2.750", saved["resource_version"])
            self.assertEqual(edited["resource_version"], "2")
            self.assertEqual(global_pricing.list_policies()["default_policy_id"], saved["id"])
            self.assertNotIn("project_id", edited)
            self.assertNotIn("platform", edited)
        self.assertFalse(self.writes())

    def test_one_default_uses_exact_cap_across_regions_and_gpu_platforms(self):
        saved = self.select()
        other = {**PROJECT, "project_id": "project-second", "region": "eu-north1"}
        self.projects.append(other)
        placements = [(PROJECT["project_id"], OFFERING["platform"]),
                      (other["project_id"], OFFERING["platform"]), (other["project_id"], "gpu-l40s-d")]
        copies = []
        for project_id, platform in placements:
            with self.subTest(project=project_id, platform=platform):
                selected = global_pricing.resolve(project_id, platform)
                self.assertEqual(selected["global_policy"]["id"], saved["id"])
                self.assertEqual(selected["policy"]["max_price"], "3.125")
                self.assertTrue(selected["create_default"])
                before = len(self.writes())
                resolved = pricing.refresh_terms(selected, project_id, platform, create=True)
                copies.append(resolved["policy"]["id"])
                self.assertEqual(len(self.writes()), before + 1)
                request = json.loads(self.writes()[-1][3])
                self.assertEqual(request["metadata"]["parent_id"], project_id)
                self.assertEqual(request["spec"]["compute_instance_spec"]["v1"]["platform"], platform)
                self.assertEqual(request["spec"]["pricing"]["max_price_v1"]["max_price"], saved["max_price"])
                self.assertEqual(self.materialize(project_id, platform)["policy"]["id"], resolved["policy"]["id"])
                self.assertEqual(len(self.writes()), before + 1)
        self.assertEqual(len(set(copies)), 3)

    def test_planning_and_preview_leave_cloud_and_saved_caps_untouched(self):
        plan = self.plan()
        core.create_gpu_vm(plan["plan_id"], dry_run=True)
        self.assertFalse((core.STATE_DIR / "global-pricing.json").exists())
        self.assertFalse(self.resources)
        self.assertFalse(self.writes())

    def test_edit_creates_new_copies_and_preserves_running_vm_caps_everywhere(self):
        saved = self.select()
        self.projects.append({**PROJECT, "project_id": "project-second", "region": "eu-north1"})
        self.materialize()
        self.materialize("project-second")
        for row in self.resources:
            row["status"]["running_vm_count"] = "2"
        before = copy.deepcopy(self.resources)
        edited = global_pricing.update_policy(saved["id"], "Everywhere", "2.000", "1")
        self.assertEqual(edited["resource_version"], "2")
        self.assertEqual(self.resources, before)
        first = self.materialize()
        second = self.materialize("project-second")
        self.assertEqual(first["policy"]["max_price"], "2.000")
        self.assertEqual(second["policy"]["max_price"], "2.000")
        self.assertEqual(self.resources[:2], before)
        self.assertFalse(any(args[2] == "update" for args in self.writes()))

    def test_global_edit_invalidates_both_planned_and_materialized_launch_reviews(self):
        saved = self.select()
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.materialize()
                plan = self.plan()
                saved = global_pricing.update_policy(saved["id"], "Everywhere", "2.000" if not existing else "1.500",
                                                     saved["resource_version"])
                before = len(self.writes())
                with patch.object(core, "ensure_ssh_key") as key, self.assertRaisesRegex(core.NebiusError, "changed since review"):
                    core.create_gpu_vm(plan["plan_id"])
                key.assert_not_called()
                self.assertEqual(len(self.writes()), before)

    def test_cloud_copy_drift_blocks_instead_of_overwriting_or_accepting_other_terms(self):
        self.select()
        reviewed = self.materialize()
        for field in ("price", "currency", "ownership"):
            before = copy.deepcopy(self.resources)
            with self.subTest(field=field):
                if field == "price":
                    self.resources[0]["spec"]["pricing"]["max_price_v1"]["max_price"] = "8.000"
                elif field == "currency":
                    self.resources[0]["status"]["currency"] = "EUR"
                else:
                    self.resources[0]["metadata"]["labels"]["managed-by"] = "another-tool"
                with self.assertRaises(core.NebiusError):
                    global_pricing.resolve(PROJECT["project_id"], OFFERING["platform"])
                with self.assertRaises(core.NebiusError):
                    pricing.refresh_terms(reviewed, PROJECT["project_id"], OFFERING["platform"], create=True)
            self.resources = before
        self.assertEqual(len(self.writes()), 1)

    def test_renamed_cloud_copy_is_reused_by_labels_and_shows_saved_name(self):
        self.select()
        first = self.materialize()
        self.resources[0]["metadata"]["name"] = "renamed-in-console"
        selected = global_pricing.resolve(PROJECT["project_id"], OFFERING["platform"])
        self.assertEqual(selected["policy"]["id"], first["policy"]["id"])
        self.assertIn("Policy: Everywhere", pricing.review_lines(selected))
        self.assertEqual(len(self.writes()), 1)

    def test_uncertain_create_reconciles_the_completed_copy_without_replay(self):
        self.select()
        selected = global_pricing.resolve(PROJECT["project_id"], OFFERING["platform"])
        def timeout_after_write(args, **kwargs):
            response = self.cli(args, **kwargs)
            if args[:3] == ["billing", "pricing-policy", "create"]:
                raise core.NebiusError("DeadlineExceeded")
            return response
        with patch.object(core, "run_cli", side_effect=timeout_after_write), self.assertRaisesRegex(core.NebiusError, "DeadlineExceeded"):
            pricing.refresh_terms(selected, PROJECT["project_id"], OFFERING["platform"], create=True)
        recovered = pricing.refresh_terms(selected, PROJECT["project_id"], OFFERING["platform"], create=True)
        self.assertEqual(recovered["policy"]["max_price"], "3.125")
        self.assertEqual(len(self.writes()), 1)

    def test_duplicate_bindings_and_unrelated_name_collision_block(self):
        self.select()
        self.materialize()
        duplicate = copy.deepcopy(self.resources[0])
        duplicate["metadata"].update(id="pricingpolicy-duplicate", name="duplicate")
        self.resources.append(duplicate)
        with self.assertRaisesRegex(core.NebiusError, "duplicates"):
            global_pricing.resolve(PROJECT["project_id"], OFFERING["platform"])
        self.resources.pop()
        self.resources[0]["metadata"]["labels"] = {}
        with self.assertRaisesRegex(core.NebiusError, "already in use"):
            global_pricing.resolve(PROJECT["project_id"], OFFERING["platform"])
        self.assertEqual(len(self.writes()), 1)

    def test_local_corruption_or_deleted_default_never_resets_to_five(self):
        saved = self.select("1.000")
        path = core.STATE_DIR / "global-pricing.json"
        original = json.loads(path.read_text())
        for value in (None, {}, {**original, "default_policy_id": None}, {**original, "default_policy_id": "spotpolicy-deleted"},
                      {**original, "policies": [{**saved, "max_price": "NaN"}]}):
            with self.subTest(value=value):
                core._atomic_json(path, value)
                with self.assertRaisesRegex(core.NebiusError, "unreadable"):
                    self.plan()
        path.write_text("not-json")
        with self.assertRaisesRegex(core.NebiusError, "unreadable"):
            self.plan()
        self.assertFalse(self.writes())

    def test_old_defaults_require_explicit_global_choice_and_can_be_imported(self):
        self.resources = [fixture.resource("My existing cap", "1.500")]
        with self.assertRaisesRegex(core.NebiusError, "older default"):
            self.plan()
        imported = global_pricing.import_policy("pricingpolicy-example", "My global cap", "1")
        with self.assertRaisesRegex(core.NebiusError, "older default"):
            self.plan()
        global_pricing.select_default(imported["id"])
        self.assertEqual(self.plan()["spot_pricing"]["policy"]["max_price"], "1.500")
        self.assertEqual(self.resources[0]["metadata"]["name"], "My existing cap")
        self.assertFalse(self.writes())

    def test_deleted_legacy_preference_requires_choice_even_without_cloud_default(self):
        core._atomic_json(core.STATE_DIR / "pricing-preferences.json",
                          {PROJECT["project_id"] + "/" + OFFERING["platform"]: "pricingpolicy-missing"})
        with self.assertRaisesRegex(core.NebiusError, "older default"):
            self.plan()
        global_pricing.select_default(global_pricing.DEFAULT_ID)
        self.assertEqual(self.plan()["spot_pricing"]["policy"]["max_price"], "5.000")

    def test_import_deduplicates_identical_caps_and_rechecks_reviewed_terms(self):
        self.projects.append({**PROJECT, "project_id": "project-second", "region": "eu-north1"})
        first = fixture.resource("Same cap", "2.000")
        second = fixture.resource("Same cap", "2.000", "pricingpolicy-second")
        second["metadata"]["parent_id"] = "project-second"
        self.resources = [first, second]
        self.assertEqual(len(global_pricing.existing_policies()["policies"]), 1)
        first["metadata"]["resource_version"] = "2"
        with self.assertRaisesRegex(core.NebiusError, "changed since review"):
            global_pricing.import_policy("pricingpolicy-example", "Imported", "1")
        first["status"]["currency"] = "EUR"
        with self.assertRaisesRegex(core.NebiusError, "USD"):
            global_pricing.import_policy("pricingpolicy-example", "Imported", "2")
        self.assertFalse(self.writes())

    def test_restart_uses_actual_vm_cap_after_global_policy_edit(self):
        saved = self.select()
        actual = self.materialize()
        instance, vm = self.stopped_vm(actual["policy"]["id"])
        global_pricing.update_policy(saved["id"], "Renamed cap", "1.000", "1")
        with patch.object(core, "_accessible_vm", return_value=(instance, vm)):
            review = pricing.review_start(vm["id"])
            self.assertEqual(review["pricing"]["policy"]["max_price"], "3.125")
            self.assertEqual(review["pricing"]["policy"]["display_name"], "Renamed cap")
            pricing.validate_start(instance, vm, review["pricing_review_id"])
        self.assertEqual(len(self.writes()), 1)

    def test_stopped_vm_can_select_global_cap_without_starting_and_checks_saved_version(self):
        saved = self.select()
        instance, vm = self.stopped_vm()
        updates = []
        def cli(args, **kwargs):
            if args[:3] == ["compute", "instance", "update"]:
                updates.append(json.loads(args[3]))
                return {}
            return self.cli(args, **kwargs)
        with patch.object(core, "_accessible_vm", return_value=(instance, vm)), patch.object(core, "run_cli", side_effect=cli):
            review = pricing.plan_configuration(vm["id"], "policy", saved["id"])
            self.assertFalse(self.writes())
            pricing.configure_vm(vm["id"], review["review_id"])
            self.assertEqual(len(updates), 1)
            self.assertTrue(updates[0]["spec"]["stopped"])
            self.assertEqual(updates[0]["metadata"]["resource_version"], "7")
            self.assertEqual(updates[0]["spec"]["spot_pricing_policy"]["id"], self.resources[0]["metadata"]["id"])
            self.assertNotIn("follows_spot_price", updates[0]["spec"])
            review = pricing.plan_configuration(vm["id"], "policy", saved["id"])
            global_pricing.update_policy(saved["id"], "Everywhere", "1.000", "1")
            with self.assertRaisesRegex(core.NebiusError, "changed since review"):
                pricing.configure_vm(vm["id"], review["review_id"])
            self.assertEqual(len(updates), 1)

    def test_custom_cap_range_rejection_does_not_allocate_or_adjust(self):
        self.select("3.125")
        plan = self.plan()
        self.assertIn("3.125", pricing.check(plan["spot_pricing"])["message"])
        self.create_error = "OutOfRange"
        with patch.object(core, "ensure_ssh_key"), patch.object(core, "_cloud_init", return_value=""), \
             patch.object(core, "validate_instance_request", return_value={"valid": True}), \
             patch.object(core, "_ssh_security_group") as security, self.assertRaisesRegex(core.NebiusError, "not adjusted"):
            core.create_gpu_vm(plan["plan_id"])
        security.assert_not_called()
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(json.loads(self.writes()[0][3])["spec"]["pricing"]["max_price_v1"]["max_price"], "3.125")

    def test_global_cli_and_agent_tools_need_no_regional_arguments(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["nebius-core", "pricing-create", "--name", "CLI cap", "--max-price", "2.000"]), redirect_stdout(output):
            self.assertEqual(core.main(), 0)
        saved = json.loads(output.getvalue())
        with patch.object(fixture.mcp.jobs, "submit", return_value={"job_id": "test"}) as submit:
            fixture.mcp._call("create_pricing_policy", {"name": "Agent cap", "max_price": "2.500"})
        self.assertEqual(submit.call_args.args[0], ["pricing-create", "--name", "Agent cap", "--max-price", "2.500"])
        fixture.mcp._call("set_default_pricing_policy", {"policy_id": saved["id"]})
        self.assertEqual(fixture.mcp._call("list_pricing_policies", {})["default_policy_id"], saved["id"])
        for tool in fixture.mcp.TOOLS:
            if tool["name"] in {"list_pricing_policies", "create_pricing_policy", "set_default_pricing_policy"}:
                self.assertNotIn("project_id", tool["inputSchema"]["properties"])
                self.assertNotIn("platform", tool["inputSchema"]["properties"])
        self.assertFalse(self.writes())


if __name__ == "__main__":
    unittest.main()
