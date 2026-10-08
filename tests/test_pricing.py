"""Public API-shaped synthetic pricing service; no account or cloud requests."""
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import nebius_core as core
import nebius_pricing as pricing
import nebius_global_pricing as global_pricing
import nebius_catalog as catalog
import nebius_agent_mcp as mcp
import nebius_ui as ui
from tests import test_compute as compute_tests
from tests.test_compute import PROJECT, OFFERING, GOOD
from tests.test_ui import app


def resource(name=None, maximum="5.000", identifier="pricingpolicy-example", platform=None):
    return {"metadata": {"id": identifier, "parent_id": PROJECT["project_id"],
                         "name": name or pricing.default_name(OFFERING["platform"]), "resource_version": "1",
                         "labels": {"managed-by": core.MANAGED_BY, "pricing-default": "true"}},
            "spec": {"compute_instance_spec": {"v1": {"platform": platform or OFFERING["platform"]}},
                     "pricing": {"max_price_v1": {"max_price": maximum}}},
            "status": {"state": "STATE_ACTIVE", "scheduling_state": "SCHEDULING_STATE_ALLOWED",
                       "running_vm_count": "0", "currency": "USD"}}


class PricingTests(unittest.TestCase):
    def setUp(self):
        compute_tests.ComputeTests.setUp(self)
        self.resources = []
        self.projects = [PROJECT]
        self.calls = []
        self.create_error = None
        self.quote_error = None
        self.quote_cost = "2.500"
        self.platform_resources = [{"metadata": {"name": OFFERING["platform"]}, "status": {"allowed_for_preemptibles": True}}]
        self.platform_error = None
        for target, name, kwargs in [
            (pricing, "require_support", {"return_value": None}),
            (core, "sync_personal_projects", {"side_effect": lambda: {"projects": self.projects}}),
            (core, "run_cli", {"side_effect": self.cli}),
            (core, "gpu_capacity", {"return_value": {"offerings": [OFFERING]}}),
            (core, "preflight_vm", {"return_value": GOOD}),
            (core, "project_context", {"return_value": PROJECT}),
        ]:
            p = patch.object(target, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def cli(self, args, **kwargs):
        self.calls.append(args)
        if args[:3] == ["compute", "platform", "list"]:
            if self.platform_error:
                raise core.NebiusError(self.platform_error)
            return {"items": self.platform_resources}
        if args[:3] == ["billing", "v1alpha1", "calculator"]:
            if self.quote_error:
                raise core.NebiusError(self.quote_error)
            return {"hourly_cost": {"general": {"total": {"cost": self.quote_cost}}}}
        if args[:2] != ["billing", "pricing-policy"]:
            raise AssertionError("Unexpected cloud call: " + repr(args))
        action = args[2]
        if action == "list":
            parent = args[args.index("--parent-id") + 1]
            return {"items": copy.deepcopy([r for r in self.resources if r["metadata"]["parent_id"] == parent])}
        if action in {"get", "get-by-name"}:
            field = "id" if action == "get" else "name"
            value = args[args.index("--" + field) + 1]
            for row in self.resources:
                if row["metadata"][field] == value and (action == "get" or
                        row["metadata"]["parent_id"] == args[args.index("--parent-id") + 1]):
                    return copy.deepcopy(row)
            raise core.NebiusError("NotFound: policy no longer exists")
        if action == "create":
            if self.create_error:
                raise core.NebiusError(self.create_error)
            request = json.loads(args[3])
            row = resource(request["metadata"]["name"], request["spec"]["pricing"]["max_price_v1"]["max_price"],
                           identifier="pricingpolicy-example" + (str(len(self.resources)) if self.resources else ""),
                           platform=request["spec"]["compute_instance_spec"]["v1"]["platform"])
            row["metadata"]["parent_id"] = request["metadata"]["parent_id"]
            row["metadata"]["labels"] = request["metadata"]["labels"]
            self.resources.append(row)
            return copy.deepcopy(row)
        if action == "update":
            row = next(r for r in self.resources if r["metadata"]["id"] == args[3])
            row["metadata"]["name"] = args[args.index("--name") + 1]
            if "--pricing-max-price-v1-max-price" in args:
                row["spec"]["pricing"]["max_price_v1"]["max_price"] = args[args.index("--pricing-max-price-v1-max-price") + 1]
            row["metadata"]["resource_version"] = "2"
            return copy.deepcopy(row)
        raise AssertionError(args)

    def plan(self, **kwargs):
        return core.plan_gpu_vm("example", OFFERING["offering_id"], PROJECT["project_id"], **kwargs)

    def writes(self):
        return [args for args in self.calls if len(args) > 2 and args[2] in {"create", "update", "delete", "start"}]

    def ui_read(self, title, command, *args):
        if command == "projects":
            return {"projects": [PROJECT]}
        if command == "pricing-platforms":
            return pricing.list_platforms(args[args.index("--project-id") + 1])
        if command == "pricing-policies":
            self.assertEqual(args, ())
            return global_pricing.list_policies()
        if command == "pricing-default":
            return global_pricing.select_default(args[args.index("--policy-id") + 1],
                                                 [args[i + 1] for i, arg in enumerate(args) if arg == "--platform"])
        if command == "pricing-existing":
            return global_pricing.existing_policies()
        raise AssertionError("Unexpected settings read: " + command)

    def test_default_plan_uses_gpu_payg_minus_one_cent_and_is_read_only(self):
        plan = self.plan()
        self.assertEqual(plan["allocation"], "preemptible")
        self.assertEqual(plan["spot_pricing"]["policy"]["max_price"], "5.390")
        self.assertTrue(plan["spot_pricing"]["create_default"])
        self.assertEqual(self.writes(), [])
        self.assertTrue(plan["preflight"]["ready"])

    def test_default_is_created_once_and_reused_across_vms(self):
        plan = self.plan()
        first = pricing.refresh_terms(plan["spot_pricing"], PROJECT["project_id"], OFFERING["platform"], create=True)
        second = self.plan()["spot_pricing"]
        self.assertEqual(first["policy"]["id"], second["policy"]["id"])
        self.assertFalse(second["create_default"])
        self.assertEqual(len(self.writes()), 1)

    def test_edited_default_is_preserved(self):
        self.resources = [resource(maximum="3.125")]
        selected = pricing.resolve(PROJECT["project_id"], OFFERING["platform"])
        self.assertEqual(selected["policy"]["max_price"], "3.125")
        self.assertFalse(self.writes())

    def test_renamed_default_is_reused_without_local_preferences(self):
        self.resources = [resource("my-renamed-default", "3.000")]
        selected = pricing.resolve(PROJECT["project_id"], OFFERING["platform"])
        self.assertEqual(selected["policy"]["name"], "my-renamed-default")
        self.assertEqual(selected["policy"]["max_price"], "3.000")
        self.assertFalse(selected["create_default"])
        self.assertFalse(self.writes())

    def test_selected_default_is_remembered_by_project_and_platform(self):
        self.resources = [resource(), resource("custom", "2.250", "pricingpolicy-custom")]
        pricing.select_default(PROJECT["project_id"], "pricingpolicy-custom")
        self.assertEqual(pricing.resolve(PROJECT["project_id"], OFFERING["platform"])["policy"]["id"], "pricingpolicy-custom")
        other = pricing.resolve(PROJECT["project_id"], "gpu-l40s-a")
        self.assertTrue(other["create_default"])
        self.assertEqual(other["policy"]["max_price"], "1.340")

    def test_deleted_preferred_policy_never_falls_back(self):
        self.resources = [resource()]
        pricing.select_default(PROJECT["project_id"], "pricingpolicy-example")
        self.resources = []
        with self.assertRaisesRegex(core.NebiusError, "NotFound"):
            pricing.resolve(PROJECT["project_id"], OFFERING["platform"])
        self.assertFalse(self.writes())

    def test_foreign_project_and_platform_are_rejected(self):
        self.resources = [resource()]
        for project, platform in [("project-other", OFFERING["platform"]), (PROJECT["project_id"], "gpu-l40s-a")]:
            with self.subTest(project=project, platform=platform), self.assertRaises(core.NebiusError):
                pricing.resolve(project, platform, "policy", "pricingpolicy-example")
        self.assertFalse(self.writes())

    def test_existing_name_collision_does_not_adopt_unrelated_default(self):
        self.resources = [resource()]
        self.resources[0]["metadata"]["labels"] = {}
        with self.assertRaisesRegex(core.NebiusError, "already in use"):
            pricing.resolve(PROJECT["project_id"], OFFERING["platform"])

    def test_limit_validation_is_decimal_and_rejects_invalid_values(self):
        self.assertEqual(pricing.amount("5"), "5.000")
        self.assertEqual(pricing.amount("0.001"), "0.001")
        for value in (5, 5.0, "0", "-1", "NaN", "Infinity", "1e2", "1.0001", "", "1,5"):
            with self.subTest(value=value), self.assertRaises(core.NebiusError):
                pricing.amount(value)

    def test_policy_names_support_existing_human_readable_names(self):
        self.assertEqual(pricing.policy_name("Development GPUs – shared cap"), "Development GPUs – shared cap")
        for name in ("", "\x1b[31mred", "a" * 1025):
            with self.assertRaises(core.NebiusError):
                pricing.policy_name(name)

    def test_out_of_range_default_does_not_allocate_disk_or_adjust_limit(self):
        plan = self.plan()
        self.create_error = "rpc error: code = OutOfRange desc = maximum exceeds allowed range"
        with patch.object(core, "ensure_ssh_key"), patch.object(core, "_cloud_init", return_value=""), \
             patch.object(core, "validate_instance_request", return_value={"valid": True}), \
             patch.object(core, "_ssh_security_group") as security:
            with self.assertRaisesRegex(core.NebiusError, "limit was not adjusted"):
                core.create_gpu_vm(plan["plan_id"])
            security.assert_not_called()
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(json.loads(self.writes()[0][3])["spec"]["pricing"]["max_price_v1"]["max_price"], "5.390")
        self.assertFalse(core._pending_launches())

    def test_range_and_expiry_errors_give_the_user_the_required_next_step(self):
        error = pricing._policy_error(core.NebiusError("OutOfRange: maximum exceeds allowed range"))
        self.assertIn("enter an allowed value", core.explain_error(str(error))["message"])
        explanation = core.explain_error("Spot pricing review expired or does not match this VM. Review again")
        self.assertIn("Review the current settings and pricing", explanation["recovery"])
        self.assertNotIn("reconnect", explanation["recovery"])

    def test_created_policy_mismatch_reports_values_before_allocating_compute(self):
        for maximum, currency in (("5.391", "USD"), ("5.390", "EUR"), ("5.390", "")):
            with self.subTest(maximum=maximum, currency=currency):
                self.resources = []
                self.calls = []
                plan = self.plan()
                def different_terms(args, **kwargs):
                    response = self.cli(args, **kwargs)
                    if args[:3] == ["billing", "pricing-policy", "create"]:
                        self.resources[-1]["spec"]["pricing"]["max_price_v1"]["max_price"] = maximum
                        self.resources[-1]["status"]["currency"] = currency
                    return response
                with patch.object(core, "run_cli", side_effect=different_terms), \
                     patch.object(core, "ensure_ssh_key"), patch.object(core, "_cloud_init", return_value=""), \
                     patch.object(core, "validate_instance_request", return_value={"valid": True}), \
                     patch.object(core, "_ssh_security_group") as security:
                    with self.assertRaises(core.NebiusError) as error:
                        core.create_gpu_vm(plan["plan_id"])
                    security.assert_not_called()
                self.assertIn("USD 5.390/GPU-hour", str(error.exception))
                self.assertIn(maximum + "/GPU-hour", str(error.exception))
                self.assertIn(currency if currency else "did not report", str(error.exception))
                self.assertEqual(len(self.writes()), 1)
                self.assertEqual(self.writes()[0][:3], ["billing", "pricing-policy", "create"])
                self.assertFalse(core._pending_launches())

    def test_unknown_create_result_is_not_replayed(self):
        self.create_error = "DeadlineExceeded"
        with self.assertRaises(core.NebiusError):
            pricing.create_policy(PROJECT["project_id"], OFFERING["platform"], "custom", "5")
        self.assertEqual(len(self.writes()), 1)
        self.resources = [resource("custom")]
        self.assertEqual(pricing.create_policy(PROJECT["project_id"], OFFERING["platform"], "custom", "5")["id"], "pricingpolicy-example")
        self.assertEqual(len(self.writes()), 1)

    def test_blocked_policy_overrides_capacity_without_mutation(self):
        self.resources = [resource()]
        self.resources[0]["status"]["scheduling_state"] = "SCHEDULING_STATE_BLOCKED"
        plan = self.plan(spot_mode="policy", pricing_policy_id="pricingpolicy-example")
        self.assertFalse(plan["preflight"]["ready"])
        with patch.object(core, "ensure_ssh_key") as key:
            with self.assertRaisesRegex(core.NebiusError, "blocked"):
                core.create_gpu_vm(plan["plan_id"])
            key.assert_not_called()
        self.assertFalse(self.writes())

    def test_policy_changes_after_review_require_new_plan(self):
        self.resources = [resource()]
        plan = self.plan(spot_mode="policy", pricing_policy_id="pricingpolicy-example")
        self.resources[0]["spec"]["pricing"]["max_price_v1"]["max_price"] = "6.000"
        self.resources[0]["metadata"]["resource_version"] = "2"
        with patch.object(core, "ensure_ssh_key") as key, self.assertRaisesRegex(core.NebiusError, "changed since review"):
            core.create_gpu_vm(plan["plan_id"])
        key.assert_not_called()
        self.assertFalse(self.writes())

    def test_limit_edit_blocks_running_vms_but_rename_preserves_limit(self):
        self.resources = [resource()]
        self.resources[0]["status"]["running_vm_count"] = "2"
        with self.assertRaisesRegex(core.NebiusError, "Stop all VMs"):
            pricing.update_policy("pricingpolicy-example", "renamed", "4", "1")
        self.assertFalse(self.writes())
        pricing.update_policy("pricingpolicy-example", "renamed", "5", "1")
        self.assertNotIn("--pricing-max-price-v1-max-price", self.writes()[0])

    def test_stale_edit_version_never_updates_shared_policy(self):
        self.resources = [resource()]
        with self.assertRaisesRegex(core.NebiusError, "changed since review"):
            pricing.update_policy("pricingpolicy-example", "renamed", "4", "0")
        self.assertFalse(self.writes())

    def test_edit_unused_policy_updates_exact_limit_and_preserves_default(self):
        self.resources = [resource()]
        result = pricing.update_policy("pricingpolicy-example", "new-name", "3.125", "1")
        self.assertEqual(result["max_price"], "3.125")
        self.assertEqual(pricing.resolve(PROJECT["project_id"], OFFERING["platform"])["policy"]["id"], "pricingpolicy-example")

    def test_unknown_currency_and_updating_policy_block_scheduling(self):
        for field, value in [("currency", ""), ("state", "STATE_UPDATING")]:
            self.resources = [resource()]
            self.resources[0]["status"][field] = value
            self.assertFalse(self.plan(spot_mode="policy", pricing_policy_id="pricingpolicy-example")["preflight"]["ready"])

    def test_on_demand_does_not_resolve_or_create_policies(self):
        plan = self.plan(allocation="on_demand")
        self.assertNotIn("spot_pricing", plan)
        self.assertEqual(self.calls, [])
        with self.assertRaisesRegex(core.NebiusError, "cannot be used"):
            self.plan(allocation="on_demand", spot_mode="follow")

    def test_policy_limit_and_calculator_price_are_separate(self):
        self.resources = [resource()]
        plan = self.plan(spot_mode="policy", pricing_policy_id="pricingpolicy-example")
        self.assertEqual(plan["spot_pricing"]["policy"]["max_price"], "5.000")
        self.assertEqual(plan["price_estimate"]["compute_per_hour"], "2.500")
        payload = json.loads(next(c[4] for c in self.calls if "calculator" in c))
        spec = payload["resource_spec"]["compute_instance_spec"]["spec"]
        self.assertEqual(spec["follows_spot_price"], {})
        self.assertNotIn("spot_pricing_policy", spec)

    def test_calculator_failure_marks_cache_stale_without_fixed_fallback(self):
        plan = self.plan()
        self.quote_error = "Service unavailable"
        estimate = pricing.estimate(plan, refresh=True)
        self.assertEqual(estimate["state"], "stale")
        self.assertEqual(estimate["checked_at"], plan["price_estimate"]["checked_at"])
        self.assertIsNone(core._hourly_estimate(OFFERING["platform"], 1, 16, 200, "preemptible"))
        self.assertIsNone(estimate["price_range"])

    def test_failed_first_quote_does_not_invent_a_price(self):
        self.quote_error = "PermissionDenied"
        plan = self.plan()
        self.assertEqual(plan["price_estimate"]["state"], "unavailable")
        self.assertIsNone(plan["estimated_usd_per_hour"])

    def test_l40s_total_is_not_mislabeled_as_gpu_unit_price(self):
        plan = self.plan()
        plan["platform"] = "gpu-l40s-a"
        self.assertIsNone(pricing.estimate(plan)["per_gpu_hour"])

    def test_requests_have_exactly_one_explicit_spot_choice(self):
        self.resources = [resource()]
        for mode in ("policy", "follow"):
            plan = self.plan(spot_mode=mode, pricing_policy_id="pricingpolicy-example" if mode == "policy" else "")
            spec = core._instance_request(plan, "computedisk-example", cloud_init="")["spec"]
            self.assertEqual(spec["recovery_policy"], "FAIL")
            self.assertEqual(spec["preemptible"], {"on_preemption": "STOP"})
            self.assertEqual(len(set(spec) & {"spot_pricing_policy", "follows_spot_price", "on_demand"}), 1)

    def test_old_spot_plan_requires_fresh_review(self):
        plan = self.plan()
        plan.pop("spot_pricing")
        core._atomic_json(core.PLAN_DIR / (plan["plan_id"] + ".json"), plan)
        with self.assertRaisesRegex(core.NebiusError, "older preemptible plan"):
            core.create_gpu_vm(plan["plan_id"], dry_run=True)
        self.assertFalse(self.writes())

    def test_policy_constrains_equivalent_platform_selection(self):
        a = {**OFFERING, "platform": "gpu-l40s-a", "offering_id": "a"}
        d = {**a, "platform": "gpu-l40s-d", "offering_id": "d", "preemptible": {"available": 99}}
        grouped = {**a, "variants": [a, d]}
        self.assertEqual(catalog.resolve_variant(grouped, PROJECT["project_id"], "preemptible", pricing_platform="gpu-l40s-a")["offering_id"], "a")

    def test_start_rechecks_policy_and_requires_matching_review(self):
        self.resources = [resource()]
        vm = {"id": "computeinstance-example", "project_id": PROJECT["project_id"], "platform": OFFERING["platform"], "state": "stopped"}
        instance = {"spec": {"preemptible": {"on_preemption": "STOP"}, "spot_pricing_policy": {"id": "pricingpolicy-example"}}}
        selected = pricing.resolve(vm["project_id"], vm["platform"], "policy", "pricingpolicy-example")
        with patch.object(pricing, "vm_details", return_value={"ready": True, "vm": vm, "pricing": selected}):
            review = pricing.review_start(vm["id"])
        pricing.validate_start(instance, vm, review["pricing_review_id"])
        self.resources[0]["status"]["scheduling_state"] = "SCHEDULING_STATE_BLOCKED"
        with patch.object(core, "_accessible_vm", return_value=(instance, vm)), patch.object(core, "_compute_mutation") as mutate:
            with self.assertRaisesRegex(core.NebiusError, "blocked"):
                core.start_vm(vm["id"], review["pricing_review_id"])
            mutate.assert_not_called()
        with self.assertRaisesRegex(core.NebiusError, "Review current"):
            pricing.validate_start(instance, vm, "")

    def test_missing_legacy_choice_is_not_treated_as_follow(self):
        self.assertEqual(pricing.instance_pricing({"spec": {"preemptible": {"on_preemption": "STOP"}}})["mode"], "legacy")

    def test_deleted_vm_policy_is_visible_for_replacement_and_blocks_start(self):
        vm = {"id": "computeinstance-example", "project_id": PROJECT["project_id"], "platform": OFFERING["platform"], "state": "stopped"}
        instance = {"metadata": {"id": vm["id"], "resource_version": "1"},
                    "spec": {"preemptible": {"on_preemption": "STOP"}, "spot_pricing_policy": {"id": "pricingpolicy-deleted"}}}
        with patch.object(core, "_accessible_vm", return_value=(instance, vm)):
            result = pricing.vm_details(vm["id"])
            self.assertFalse(result["ready"])
            self.assertIn("NotFound", result["pricing_error"])
            self.assertIn("review_id", pricing.plan_configuration(vm["id"], "follow"))

    def test_review_shows_gpu_multiplier_only_when_count_is_known(self):
        choice = {"mode": "policy", "policy": pricing.policy_row(resource())}
        self.assertIn("USD 40.000/hour", "\n".join(pricing.review_lines(choice, gpu_count=8)))
        self.assertNotIn("limit for this VM", "\n".join(pricing.review_lines(choice)))

    def test_stopped_vm_pricing_change_uses_reviewed_version_and_does_not_start(self):
        self.resources = [resource()]
        vm = {"id": "computeinstance-example", "project_id": PROJECT["project_id"], "platform": OFFERING["platform"], "state": "stopped"}
        instance = {"metadata": {"id": vm["id"], "parent_id": vm["project_id"], "resource_version": "7"},
                    "spec": {"preemptible": {"on_preemption": "STOP"}, "follows_spot_price": {}, "stopped": True}}
        with patch.object(core, "_accessible_vm", return_value=(instance, vm)):
            review = pricing.plan_configuration(vm["id"], "policy", "pricingpolicy-example")
            self.assertFalse(self.writes())
            with patch.object(core, "run_cli", side_effect=lambda args, **kwargs: {} if args[:3] == ["compute", "instance", "update"] else self.cli(args, **kwargs)) as cli:
                pricing.configure_vm(vm["id"], review["review_id"])
            calls = [c.args[0] for c in cli.call_args_list if c.args[0][:3] == ["compute", "instance", "update"]]
            request = json.loads(calls[0][3])
            self.assertEqual(request["metadata"]["resource_version"], "7")
            self.assertEqual(request["spec"]["spot_pricing_policy"]["id"], "pricingpolicy-example")
            self.assertNotIn("follows_spot_price", request["spec"])
            self.assertTrue(request["spec"]["stopped"])

    def test_vm_change_review_cannot_survive_concurrent_vm_edit(self):
        vm = {"id": "computeinstance-example", "project_id": PROJECT["project_id"], "platform": OFFERING["platform"], "state": "stopped"}
        instance = {"metadata": {"id": vm["id"], "resource_version": "1"}, "spec": {"preemptible": {"on_preemption": "STOP"}}}
        with patch.object(core, "_accessible_vm", return_value=(instance, vm)):
            review = pricing.plan_configuration(vm["id"], "follow")
            instance["metadata"]["resource_version"] = "2"
            with self.assertRaisesRegex(core.NebiusError, "changed"):
                pricing.configure_vm(vm["id"], review["review_id"])
        self.assertFalse(self.writes())

    def test_mcp_default_and_read_write_contracts(self):
        tools = {tool["name"]: tool for tool in mcp.TOOLS}
        self.assertEqual(tools["plan_gpu_vm"]["inputSchema"]["properties"]["allocation"]["default"], "preemptible")
        self.assertTrue(tools["review_vm_start"]["annotations"]["readOnlyHint"])
        for name in ("create_pricing_policy", "update_pricing_policy", "apply_vm_pricing"):
            self.assertFalse(tools[name]["annotations"]["readOnlyHint"])
        with patch.object(mcp.jobs, "submit", return_value={"job_id": "test"}) as submit:
            mcp._call("start_vm", {"vm_id": "computeinstance-example", "pricing_review_id": "review"})
        self.assertIn("review", submit.call_args.args[0])

    def test_policy_picker_is_keyboard_operable_at_narrow_width(self):
        row = global_pricing.list_policies()["policies"][0]
        application, screen = app(["2"], width=48, height=20)
        with patch.object(application, "read", return_value={"policies": [row], "default_policy_ids": {}, "range_note": "Allowed range: console"}), \
             patch.object(application, "mutate") as mutate:
            choice = application.choose_spot_pricing()
        self.assertEqual(choice["policy_id"], row["id"])
        self.assertIsNone(choice["platform"])
        mutate.assert_not_called()

    def test_settings_is_reachable_and_browsing_requires_no_capacity_or_vm(self):
        self.resources = [resource()]
        for width, height in [(48, 20), (80, 30)]:
            # Settings -> global policies -> back; no project or platform menus.
            application, screen = app(["p", "\x1b", "\x1b"], width, height)
            with patch.object(application, "read", side_effect=self.ui_read), \
                 patch.object(application, "agent_status", return_value={"installed": False, "ready": False}), \
                 patch.object(application, "mutate") as mutate, \
                 patch.object(core, "inventory_preferences", return_value={"include_kubernetes_nodes": False}), \
                 patch.object(core, "gpu_capacity", side_effect=AssertionError("Settings must not require capacity")), \
                 self.assertRaises(ui.Back):
                application.preferences()
            self.assertIn("Spot pricing policies", "\n".join(screen.frames))
            self.assertIn("Settings", screen.frames[-1])
            self.assertNotIn("[ On-demand | Preemptible ]", screen.frames[0])
            self.assertNotIn("Choose a project", "\n".join(screen.frames))
            self.assertNotIn("Choose the GPU platform", "\n".join(screen.frames))
            mutate.assert_not_called()
        self.assertFalse(self.writes())

    def test_settings_default_is_reused_by_the_launch_picker_and_plan(self):
        saved = global_pricing.create_policy("my-saved-policy", "3.125")
        application, screen = app(["7", "2", "4", "\x1b", "\n"], 80, 30)
        with patch.object(application, "read", side_effect=self.ui_read):
            with self.assertRaises(ui.Back):
                application.manage_spot_policies()
            selection = application.choose_spot_pricing(OFFERING["platform"])
        self.assertEqual(selection["mode"], "default")
        self.assertIn("3.125/GPU-hour", screen.frames[-1])
        self.assertEqual(global_pricing.list_policies()["default_policy_ids"][OFFERING["platform"]], saved["id"])
        self.assertEqual(self.plan()["spot_pricing"]["policy"]["max_price"], "3.125")
        self.assertFalse(self.writes())

    def test_launch_can_open_management_and_return_without_changing_its_choice(self):
        application, _ = app(["e", "\x1b", "\n"], 80, 30)
        with patch.object(application, "read", side_effect=self.ui_read), patch.object(application, "mutate") as mutate:
            selection = application.choose_spot_pricing()
        self.assertEqual(selection["mode"], "default")
        mutate.assert_not_called()

    def test_rtx_picker_shows_its_default_and_omits_caps_above_its_maximum(self):
        saved = global_pricing.create_policy("Small custom cap", "1.200")
        application, screen = app(["\n"], 80, 40)
        with patch.object(application, "read", side_effect=self.ui_read):
            selection = application.choose_spot_pricing("gpu-rtx6000-a")
        self.assertEqual(selection["mode"], "default")
        self.assertIn("RTX PRO 6000 default · USD 1.790/GPU-hour", screen.frames[-1])
        self.assertIn(saved["name"], screen.frames[-1])
        self.assertNotIn("B300 default", screen.frames[-1])
        self.assertNotIn("H100 default", screen.frames[-1])
        self.assertFalse(self.writes())

    def test_settings_creation_requires_review_and_needs_no_project_or_platform(self):
        for approved in (False, True):
            application, _ = app(["n", "\x1b"], 80, 30)
            with patch.object(application, "read", side_effect=self.ui_read), \
                 patch.object(application, "edit", side_effect=["Shared development", "4.000"]), \
                 patch.object(application, "confirm_launch", return_value=approved), \
                 patch.object(application, "mutate") as mutate, self.assertRaises(ui.Back):
                application.manage_spot_policies()
            if approved:
                mutate.assert_called_once_with("Saving pricing policy", "pricing-create",
                    "--name", "Shared development", "--max-price", "4.000")
            else:
                mutate.assert_not_called()

    def test_settings_edit_reviews_future_launch_cap_and_version(self):
        self.resources = [resource()]
        self.resources[0]["status"]["running_vm_count"] = "2"
        application, _ = app(["1", "1", "\x1b"], 80, 30)
        with patch.object(application, "read", side_effect=self.ui_read), \
             patch.object(application, "edit", side_effect=["Renamed policy", "3.125"]), \
             patch.object(application, "confirm_launch", return_value=True) as review, \
             patch.object(application, "mutate") as mutate, self.assertRaises(ui.Back):
            application.manage_spot_policies()
        self.assertIn("Existing VMs keep their cap", "\n".join(review.call_args.args[0]))
        arguments = mutate.call_args.args
        self.assertEqual(arguments[arguments.index("--max-price") + 1], "3.125")
        self.assertEqual(arguments[arguments.index("--expected-version") + 1], "1")

    def test_platform_discovery_keeps_saved_policies_on_unavailable_platforms(self):
        self.resources = [resource(platform="gpu-l40s-d")]
        self.platform_resources += [
            {"metadata": {"name": "cpu-d3"}},
            {"metadata": {"name": "gpu-l40s-d"}, "status": {"allowed_for_preemptibles": False}},
            {"metadata": {"name": "gpu-b200-sxm"}, "status": {"allowed_for_preemptibles": False}},
        ]
        result = pricing.list_platforms(PROJECT["project_id"])
        self.assertEqual(result["platforms"], sorted([OFFERING["platform"], "gpu-l40s-d"]))
        self.assertFalse(self.writes())
        self.platform_error = "Unavailable"
        result = pricing.list_platforms(PROJECT["project_id"])
        self.assertEqual(result["platforms"], ["gpu-l40s-d"])
        self.assertTrue(result["warnings"])

    def test_settings_work_without_personal_projects_or_cloud_access(self):
        application, screen = app(["\x1b"])
        with patch.object(application, "read", side_effect=self.ui_read), \
             patch.object(core, "run_cli", side_effect=AssertionError("Settings must work offline")), \
             patch.object(core, "sync_personal_projects", side_effect=AssertionError("No project required")), \
             patch.object(application, "mutate") as mutate, self.assertRaises(ui.Back):
            application.pricing_settings()
        self.assertIn("Defaults by GPU", screen.frames[-1])
        mutate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
