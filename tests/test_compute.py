"""Regression tests for cloud mutation boundaries and the reported quota failure."""

import copy
import datetime as dt
import fcntl
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import nebius_core as core
import nebius_agent_mcp as mcp


PROJECT = {"project_id": "project-personal", "project_name": "personal", "region": "eu-west1",
           "tenant_id": "tenant-test", "tenant_name": "Test", "subnet_id": "vpcsubnet-test", "subnet_name": "default"}
OFFERING = {"offering_id": "choice", "platform": "gpu-h200-sxm", "preset": "1gpu-16vcpu-200gb",
            "gpu_count": 1, "vcpu_count": 16, "memory_gib": 200, "region": "eu-west1", "fabric": None,
            "preemptible": {"available": 3, "level": "high"}, "on_demand": {"available": 1, "level": "low"},
            "projects": [PROJECT]}
GOOD = {"ready": True, "checks": [], "warnings": [], "region": "eu-west1", "message": "Checked"}


class ComputeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = Path(self.temp.name)
        for key, value in {
            "STATE_DIR": state, "PLAN_DIR": state / "plans", "REGISTRY_FILE": state / "vms.json",
            "OPERATION_FILE": state / "operation.json", "PROJECTS_FILE": state / "projects.json",
            "CAPACITY_FILE": state / "capacity.json", "INVENTORY_FILE": state / "inventory.json",
            "CONNECTIONS_FILE": state / "connections.json", "PREFERENCES_FILE": state / "preferences.json",
            "SSH_KEY": state / "id_key",
        }.items():
            p = patch.object(core, key, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(core, "profile_value", side_effect=lambda field: "tenant-test" if field == "tenant-id" else "project-personal")
        p.start()
        self.addCleanup(p.stop)

    def plan(self, allocation="on_demand", offering=None):
        with patch.object(core, "gpu_capacity", return_value={"offerings": [offering or copy.deepcopy(OFFERING)]}), \
             patch.object(core, "preflight_vm", return_value=GOOD):
            return core.plan_gpu_vm("training-box", "choice", "project-personal", allocation, 0)

    def test_selected_image_and_disk_size_reach_plan_cost_and_disk_request(self):
        from tests.test_catalog import IMAGE
        import nebius_catalog as catalog
        with patch.object(core, "gpu_capacity", return_value={"offerings": [OFFERING]}), \
             patch.object(core, "preflight_vm", return_value=GOOD) as preflight, \
             patch.object(catalog, "get_image", return_value=IMAGE):
            plan = core.plan_gpu_vm("training-box", "choice", "project-personal", image_id="computeimage-custom")
        self.assertEqual(plan["image_id"], "computeimage-custom")
        self.assertEqual(plan["image_family"], "")
        self.assertEqual(plan["disk_gib"], 256)
        self.assertEqual(preflight.call_args.kwargs["disk_gib"], 256)
        self.assertEqual(preflight.call_args.kwargs["image_id"], "computeimage-custom")
        command = core._disk_arguments(plan)
        self.assertEqual(command[command.index("--source-image-id") + 1], "computeimage-custom")
        self.assertNotIn("--source-image-family-image-family", command)
        self.assertEqual(plan["estimated_usd_per_hour"], core._hourly_estimate(
            OFFERING["platform"], 1, 16, 200, "on_demand", 256))

    def test_selected_image_with_too_small_disk_cannot_plan(self):
        from tests.test_catalog import IMAGE
        import nebius_catalog as catalog
        with patch.object(core, "gpu_capacity", return_value={"offerings": [OFFERING]}), \
             patch.object(core, "run_cli") as cli, patch.object(catalog, "get_image", return_value=IMAGE):
            with self.assertRaisesRegex(core.NebiusError, "at least 256"):
                core.plan_gpu_vm("training-box", "choice", "project-personal", image_id="computeimage-custom", disk_gib=200)
            cli.assert_not_called()
        self.assertFalse(core.PLAN_DIR.exists())

    def test_revoked_image_is_blocked_before_disk_or_instance_create(self):
        import nebius_catalog as catalog
        plan = self.plan()
        plan.update(image_id="computeimage-revoked", image_family="", disk_gib=256)
        core._atomic_json(core.PLAN_DIR / (plan["plan_id"] + ".json"), plan)
        with patch.object(core, "project_context", return_value=PROJECT), \
             patch.object(core, "run_cli", side_effect=self.preflight_cli(ssd_limit=256)) as cli, \
             patch.object(catalog, "get_image", side_effect=core.NebiusError("Image access denied")):
            with self.assertRaisesRegex(core.NebiusError, "Preflight failed"):
                core.create_gpu_vm(plan["plan_id"])
        self.assertFalse(any('create' in call.args[0] for call in cli.call_args_list))

    def test_saved_disk_from_another_image_is_not_reused(self):
        plan = self.plan()
        saved = {"project": plan["project"], "image_family": "", "image_id": "computeimage-old", "disk_gib": 256}
        plan.update(image_id="computeimage-new", image_family="", disk_gib=256)
        with patch.object(core, "_reusable_disks", return_value=[saved]), \
             patch.object(core, "_verify_reusable_disk") as verify:
            self.assertIsNone(core._select_reusable_disk(plan))
            verify.assert_not_called()

    def test_allocation_is_carried_into_exact_instance_request(self):
        for allocation in ("on_demand", "preemptible"):
            plan = self.plan(allocation)
            with patch.object(core, "_cloud_init", return_value="#cloud-config"):
                request = core._instance_request(plan, "computedisk-test")
            self.assertEqual(plan["allocation"], allocation)
            self.assertNotIn("auto_stop_hours", plan)
            self.assertNotIn("auto-stop-hours", request["metadata"]["labels"])
            self.assertIn("Stop the VM manually", plan["runtime_note"])
            if allocation == "on_demand":
                self.assertNotIn("preemptible", request["spec"])
                self.assertEqual(request["spec"]["reservation_policy"], {"policy": "FORBID"})
            else:
                self.assertEqual(request["spec"]["preemptible"]["on_preemption"], "STOP")
                self.assertEqual(request["spec"]["recovery_policy"], "FAIL")
            self.assertEqual(request["spec"]["boot_disk"]["attach_mode"], "READ_WRITE")
            interface = request["spec"]["network_interfaces"][0]
            # The API name is used for cloud-init's guest interface rename.
            # "default" passed ProtoJSON validation but broke first-boot networking.
            self.assertEqual(interface["name"], "eth0")
            self.assertEqual(interface["public_ip_address"], {"static": True})
            self.assertEqual(len(interface["security_groups"]), 1)

    def test_unavailable_mode_is_not_rescued_by_other_modes_capacity(self):
        offering = copy.deepcopy(OFFERING)
        offering["on_demand"] = {"available": 0, "level": "high"}
        with self.assertRaises(core.NebiusError):
            self.plan("on_demand", offering)
        self.assertEqual(self.plan("preemptible", offering)["allocation"], "preemptible")

    def test_missing_personal_project_does_not_index_an_empty_list(self):
        offering = copy.deepcopy(OFFERING)
        offering["projects"] = []
        with self.assertRaisesRegex(core.NebiusError, "No personal project"):
            self.plan(offering=offering)

    def test_zero_regional_ssd_quota_is_blocked(self):
        quota = {"metadata": {"name": "compute.disk.size.network-ssd"},
                 "spec": {"region": "uk-south2", "limit": "0"},
                 "status": {"usage": "0", "state": "STATE_ACTIVE", "usage_state": "USAGE_STATE_NOT_USED"}}
        with patch.object(core, "run_cli", return_value={"items": [quota]}):
            result = core.preflight_vm("uk-south2")
            self.assertFalse(result["ready"])
            self.assertIn("0 GiB quota available", result["message"])
            self.assertTrue(core.preflight_vm("eu-west1")["ready"])
            self.assertTrue(core.preflight_vm("eu-west1")["warnings"])

    def test_usage_and_project_quota_are_checked(self):
        quotas = {"items": [{"metadata": {"name": "compute.disk.size.network-ssd"},
                            "spec": {"region": "eu-west1", "limit": 250 * 1024**3},
                            "status": {"usage": 100 * 1024**3}}]}
        with patch.object(core, "run_cli", return_value=quotas) as cli:
            result = core.preflight_vm("eu-west1", project_id="project-personal")
            self.assertFalse(result["ready"])
            self.assertEqual(cli.call_count, 2)

    def test_zero_quota_blocks_even_when_usage_unknown(self):
        quota = {"metadata": {"name": "compute.disk.size.network-ssd"},
                 "spec": {"region": "eu-west1", "limit": "0"},
                 "status": {"usage_state": "USAGE_STATE_UNKNOWN"}}
        with patch.object(core, "run_cli", return_value={"items": [quota]}):
            self.assertFalse(core.preflight_vm("eu-west1")["ready"])

    def test_obsolete_auto_stop_is_always_a_noop_even_when_overdue(self):
        core._save_registry({"vms": [{"id": "computeinstance-test", "auto_stop_hours": 2,
                                     "auto_stop_at": "2020-01-01T00:00:00+00:00"}]})
        with patch.object(core, "_accessible_vm") as access, patch.object(core, "run_cli") as cli:
            self.assertTrue(core.stop_vm("computeinstance-test", automatic=True)["skipped"])
            access.assert_not_called()
            cli.assert_not_called()

    def test_mutation_guard_is_reentrant_for_worker(self):
        with patch.object(core.fcntl, "flock") as lock:
            with core.mutation_guard():
                with core.mutation_guard():
                    pass
            self.assertEqual([call.args[1] for call in lock.call_args_list],
                             [fcntl.LOCK_SH | fcntl.LOCK_NB, fcntl.LOCK_EX | fcntl.LOCK_NB])

    def test_uninstall_lock_blocks_new_cloud_mutations(self):
        with (core.STATE_DIR / "uninstall.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(core.NebiusError, "being uninstalled"):
                with core.mutation_guard():
                    self.fail("a cloud mutation entered during uninstall")

    def test_create_rechecks_quota_before_creating_disk(self):
        plan = self.plan()
        blocked = {**GOOD, "ready": False, "message": "SSD storage quota exceeded", "recovery": "Choose another region"}
        with patch.object(core, "project_context", return_value=PROJECT), \
             patch.object(core, "preflight_vm", return_value=blocked), \
             patch.object(core, "run_cli") as cli:
            with self.assertRaisesRegex(core.NebiusError, "Preflight failed"):
                core.create_gpu_vm(plan["plan_id"])
            cli.assert_not_called()

    def test_project_without_confirmation_does_not_mutate(self):
        with patch.object(core, "gpu_capacity", return_value={"offerings": [OFFERING]}), \
             patch.object(core, "run_cli") as cli:
            with self.assertRaisesRegex(core.NebiusError, "confirmation"):
                core.create_nebius_project("eu-west1", name="my-project")
            cli.assert_not_called()

    def test_project_collision_keeps_user_name_and_does_not_create(self):
        with patch.object(core, "gpu_capacity", return_value={"offerings": [OFFERING]}), \
             patch.object(core, "preflight_vm", return_value=GOOD), \
             patch.object(core, "_tenant_user_id", return_value="tenantuseraccount-test"), \
             patch.object(core, "run_cli", return_value={"items": [{"metadata": {"name": "my-project"}}]}) as cli:
            with self.assertRaisesRegex(core.NebiusError, "already exists"):
                core.create_nebius_project("eu-west1", name="my-project", confirmed=True)
            self.assertEqual(cli.call_count, 1)

    def test_project_is_recorded_before_network_failure(self):
        def cli(args, **kwargs):
            if args[:3] == ["iam", "project", "list"]:
                return {"items": []}
            if args[:3] == ["iam", "project", "create"]:
                self.assertIn("my-project", args)
                return {"metadata": {"id": "project-new"}}
            raise core.NebiusError("Network service unavailable")
        with patch.object(core, "gpu_capacity", return_value={"offerings": [OFFERING]}), \
             patch.object(core, "preflight_vm", return_value=GOOD), \
             patch.object(core, "_tenant_user_id", return_value="tenantuseraccount-test"), \
             patch.object(core, "run_cli", side_effect=cli):
            with self.assertRaises(core.NebiusError):
                core.create_nebius_project("eu-west1", name="my-project", confirmed=True)
        self.assertEqual(core._personal_project_registry()["projects"][0]["project_id"], "project-new")
        self.assertEqual(core._read_json(core.OPERATION_FILE, {})["project_id"], "project-new")

    def test_timed_out_create_preserves_disk_and_prevents_plan_replay(self):
        plan = self.plan()
        def cli(args, **kwargs):
            if args[:3] == ["compute", "disk", "create"]:
                return {"metadata": {"id": "computedisk-kept"}}
            if args[:3] == ["compute", "instance", "create"]:
                raise core.NebiusError("timed out")
            self.fail(f"Unexpected call: {args}")
        with patch.object(core, "project_context", return_value=PROJECT), \
             patch.object(core, "_ssh_security_group", return_value="vpcsecuritygroup-ssh"), \
             patch.object(core, "preflight_vm", return_value=GOOD), \
             patch.object(core, "validate_instance_request", return_value={"valid": True}), \
             patch.object(core, "ensure_ssh_key"), patch.object(core, "_cloud_init", return_value=""), \
             patch.object(core, "run_cli", side_effect=cli):
            with self.assertRaises(core.NebiusError):
                core.create_gpu_vm(plan["plan_id"])
            with self.assertRaisesRegex(core.NebiusError, "already been submitted"):
                core.create_gpu_vm(plan["plan_id"])
        operation = core._read_json(core.OPERATION_FILE, {})
        self.assertEqual(operation["disk_id"], "computedisk-kept")
        self.assertIn("may still finish", operation["recovery"])
        pending = core._pending_launches()
        self.assertEqual(pending[0]["disk_id"], "computedisk-kept")

    def test_interrupted_launch_can_recover_exact_vm(self):
        plan = self.plan()
        plan["submitted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        plan["disk_id"] = "computedisk-kept"
        core._atomic_json(core._pending_path(plan["plan_id"]), plan)
        item = {"metadata": {"id": "computeinstance-found", "labels": {
            "managed-by": core.MANAGED_BY, "request-id": plan["plan_id"]}},
            "spec": {"boot_disk": {"existing_disk": {"id": "computedisk-kept"}}}}
        with patch.object(core, "sync_personal_projects", return_value={"projects": [PROJECT]}), \
             patch.object(core, "run_cli", return_value={"items": [item]}) as cli:
            vm = core.recover_launch(plan["plan_id"])
            self.assertEqual(vm["ssh_user"], core.SSH_USER)
            self.assertEqual(core._registered(vm["id"])["disk_id"], "computedisk-kept")
            self.assertFalse(core._pending_launches())
            self.assertEqual(cli.call_count, 1)

    def test_unconfirmed_recovery_never_creates_or_deletes(self):
        plan = self.plan()
        core._atomic_json(core._pending_path(plan["plan_id"]), plan)
        with patch.object(core, "sync_personal_projects", return_value={"projects": [PROJECT]}), \
             patch.object(core, "run_cli", return_value={"items": []}) as cli:
            with self.assertRaisesRegex(core.NebiusError, "still unconfirmed"):
                core.recover_launch(plan["plan_id"])
            self.assertTrue(core._pending_launches())
            self.assertTrue(all(call.args[0][2] == "list" for call in cli.call_args_list))
        with self.assertRaises(core.NebiusError):
            core.archive_request(plan["plan_id"])

    def test_remaining_disk_is_visible_in_inventory(self):
        core._save_registry({"vms": [{"id": "computeinstance-gone", "disk_id": "computedisk-kept", "name": "old",
                                     "project_id": PROJECT["project_id"], "instance_deleted": True}]})
        with patch.object(core, "sync_personal_projects", return_value={"projects": [PROJECT], "hidden_shared_project_count": 0}), \
             patch.object(core, "run_cli", return_value={"items": []}):
            vm = core.list_vms(force_refresh=True)["vms"][0]
            self.assertEqual(vm["state"], "disk remains")
            self.assertTrue(vm["can_delete"])

    def test_kubernetes_nodes_are_hidden_by_default_and_can_be_included(self):
        normal = {"id": "computeinstance-normal", "state": "running", "service_managed_by": "",
                  "kubernetes_node": False}
        node = {"id": "computeinstance-node", "state": "running",
                "service_managed_by": "mk8snodegroup-workers", "kubernetes_node": True}
        snapshot = {"vms": [normal, node]}

        hidden = core._visible_inventory(snapshot)
        self.assertEqual(hidden["vms"], [normal])
        self.assertEqual(hidden["hidden_kubernetes_node_count"], 1)
        self.assertFalse(hidden["include_kubernetes_nodes"])

        core.set_include_kubernetes_nodes(True)
        shown = core._visible_inventory(snapshot)
        self.assertEqual(shown["vms"], [normal, node])
        self.assertEqual(shown["hidden_kubernetes_node_count"], 0)
        self.assertTrue(shown["include_kubernetes_nodes"])

    def test_kubernetes_owner_is_detected_without_name_heuristics(self):
        base = {"metadata": {"id": "computeinstance-test", "name": "ordinary-looking"},
                "spec": {"resources": {}}, "status": {"managed_by": "mk8snodegroup-workers"}}
        summary = core._vm_summary(base, PROJECT)
        self.assertTrue(summary["kubernetes_node"])
        self.assertFalse(summary["can_delete"])
        labelled = {"metadata": {"labels": {"nebius.com/node-group-id": "mk8snodegroup-workers"}}, "status": {}}
        self.assertTrue(core._is_kubernetes_node(labelled))
        current_label = {"metadata": {"labels": {"mk8s-node-group-id": "mk8snodegroup-workers"}}, "status": {}}
        self.assertTrue(core._is_kubernetes_node(current_label))
        self.assertFalse(core._is_kubernetes_node({"metadata": {"name": "kubernetes-demo"}, "status": {}}))

    def test_inventory_from_before_kubernetes_classification_is_refreshed(self):
        core._atomic_json(core.INVENTORY_FILE, {
            "schema": "nebius.inventory/v1", "tenant_id": "tenant-test",
            "vms": [{"id": "computeinstance-stale", "name": "stale worker"}],
        })
        node = {
            "metadata": {"id": "computeinstance-node", "name": "worker", "labels": {
                "mk8s-node-group-id": "mk8snodegroup-workers",
            }},
            "spec": {"resources": {}}, "status": {"state": "RUNNING"},
        }
        personal = {"projects": [PROJECT], "hidden_shared_project_count": 0}
        with patch.object(core, "sync_personal_projects", return_value=personal), \
             patch.object(core, "run_cli", return_value={"items": [node]}) as cli:
            result = core.list_vms()
        self.assertEqual(result["vms"], [])
        self.assertEqual(result["hidden_kubernetes_node_count"], 1)
        self.assertEqual(result["source"], "live")
        self.assertEqual(core._read_json(core.INVENTORY_FILE, {})["schema"], core.INVENTORY_SCHEMA)
        cli.assert_called_once()

    def test_service_managed_vm_lifecycle_is_not_exposed_as_direct_compute(self):
        vm = {"id": "computeinstance-node", "name": "worker", "service_managed_by": "mk8snodegroup-workers"}
        with patch.object(core, "_accessible_vm", return_value=({}, vm)), \
             patch.object(core, "_compute_mutation") as mutate:
            with self.assertRaisesRegex(core.NebiusError, "managed by another Nebius service"):
                core.stop_vm(vm["id"])
            with self.assertRaisesRegex(core.NebiusError, "managed by another Nebius service"):
                core.start_vm(vm["id"])
        mutate.assert_not_called()

    def test_dry_run_does_not_generate_key_or_mutate_cloud(self):
        plan = self.plan()
        with patch.object(core, "project_context", return_value=PROJECT), \
             patch.object(core, "ensure_ssh_key") as key, patch.object(core, "run_cli") as cli:
            self.assertTrue(core.create_gpu_vm(plan["plan_id"], dry_run=True)["dry_run"])
            key.assert_not_called()
            cli.assert_not_called()

    def test_external_ssh_uses_agent_and_validates_username(self):
        vm = {"state": "running", "public_ip": "192.0.2.1", "name": "existing", "managed": False}
        with patch.object(core, "_accessible_vm", return_value=({}, vm)):
            result = core.connect_vm("computeinstance-existing", launch=False, username="ubuntu")
            self.assertNotIn("-i", result["command"])
            self.assertEqual(result["command"][-1], "ubuntu@192.0.2.1")
            with self.assertRaisesRegex(core.NebiusError, "Invalid SSH username"):
                core.connect_vm("computeinstance-existing", launch=False, username="-oProxyCommand=evil")

    def test_external_vm_delete_requires_confirmation_without_cli_calls(self):
        with patch.object(core, "run_cli") as cli:
            with self.assertRaises(core.NebiusError):
                core.delete_vm("computeinstance-external", False)
            cli.assert_not_called()

    def test_mcp_advertises_allocation_and_uses_generic_names(self):
        tools = {tool["name"]: tool for tool in mcp.TOOLS}
        self.assertIn("create_project", tools)
        self.assertIn("check_vm_quota", tools)
        self.assertEqual(tools["plan_gpu_vm"]["inputSchema"]["properties"]["allocation"]["enum"], ["on_demand", "preemptible"])
        self.assertTrue(tools["delete_vm"]["annotations"]["destructiveHint"])

    def rejected_launch(self, error='Nebius: Error: read protojson from positional arguments: invalid value for enum field attachMode: "read_write"'):
        plan = self.plan("preemptible")
        plan["disk_id"] = "computedisk-kept"
        plan["submitted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        core._atomic_json(core._pending_path(plan["plan_id"]), plan)
        job = {"phase": "error", "command": "create", "error": error,
               "operation": {"disk_id": plan["disk_id"], "name": plan["name"], "project": plan["project"]}}
        (core.STATE_DIR / "activity.log").write_text(json.dumps(job) + "\n")
        disk = {"metadata": {"id": plan["disk_id"], "parent_id": PROJECT["project_id"], "name": plan["name"] + "-boot",
                             "labels": {"managed-by": core.MANAGED_BY, "request-id": plan["plan_id"]}},
                "status": {"state": "READY", "size_bytes": str(200 * 1024**3), "source_image_id": "computeimage-test", "lock_state": {}}}
        return plan, disk

    def test_verified_local_rejection_clears_block_without_cloud_writes(self):
        plan, disk = self.rejected_launch()
        with patch.object(core, "run_cli", side_effect=[disk, {"items": []}]) as cli:
            result = core.repair_rejected_launches()
        self.assertEqual(result["resolved"][0]["disk_id"], plan["disk_id"])
        self.assertFalse(result["cloud_resources_changed"])
        self.assertFalse(core._pending_launches())
        self.assertEqual(len(core._reusable_disks()), 1)
        self.assertTrue((core.STATE_DIR / "resolved-requests" / f"{plan['plan_id']}.json").is_file())
        self.assertTrue(all(call.args[0][2] in {"get", "list"} for call in cli.call_args_list))

    def test_timeout_or_remote_validation_error_never_clears_recovery(self):
        for error in ("Nebius: timed out", "Error: rpc error: code = InvalidArgument desc = read protojson from positional arguments: bad"):
            plan, disk = self.rejected_launch(error)
            with patch.object(core, "run_cli") as cli:
                self.assertFalse(core.repair_rejected_launches()["resolved"])
                cli.assert_not_called()
            self.assertTrue(core._pending_path(plan["plan_id"]).exists())

    def test_repair_refuses_attached_or_wrongly_owned_disk(self):
        plan, disk = self.rejected_launch()
        attached = {"items": [{"spec": {"secondary_disks": [{"existing_disk": {"id": plan["disk_id"]}}]}}]}
        with patch.object(core, "run_cli", side_effect=[disk, attached]):
            with self.assertRaisesRegex(core.NebiusError, "already uses"):
                core.repair_rejected_launches()
        disk["metadata"]["labels"]["request-id"] = "not-this-request"
        with patch.object(core, "run_cli", return_value=disk):
            with self.assertRaisesRegex(core.NebiusError, "ownership"):
                core.repair_rejected_launches()
        self.assertTrue(core._pending_path(plan["plan_id"]).exists())

    def test_reused_disk_is_reviewed_and_never_created_twice(self):
        old, disk = self.rejected_launch()
        with patch.object(core, "run_cli", side_effect=[disk, {"items": []}]):
            core.repair_rejected_launches()
        with patch.object(core, "run_cli", side_effect=[disk, {"items": []}]):
            plan = self.plan("on_demand")
        self.assertEqual(plan["reusable_disk"]["disk_id"], disk["metadata"]["id"])
        calls = []
        def cli(args, **kwargs):
            calls.append(args)
            if args[:3] == ["compute", "disk", "get"]:
                return disk
            if args[:3] == ["compute", "instance", "list"]:
                return {"items": []}
            if args[:3] == ["compute", "instance", "create"]:
                request = json.loads(args[3])
                self.assertEqual(request["spec"]["boot_disk"]["existing_disk"]["id"], old["disk_id"])
                self.assertEqual(request["spec"]["reservation_policy"]["policy"], "FORBID")
                return {"metadata": {"id": "computeinstance-new"}}
            self.fail(f"Unexpected cloud operation: {args[:3]}")
        with patch.object(core, "run_cli", side_effect=cli), patch.object(core, "project_context", return_value=PROJECT), \
             patch.object(core, "_ssh_security_group", return_value="vpcsecuritygroup-ssh"), \
             patch.object(core, "preflight_vm", return_value=GOOD) as quota, patch.object(core, "ensure_ssh_key"), \
             patch.object(core, "_cloud_init", return_value="#cloud-config"), \
             patch.object(core, "validate_instance_request", return_value={"valid": True}) as validator, \
             patch.object(core, "_wait_for_instance", return_value={"state": "running", "public_ip": "192.0.2.5"}), \
             patch.object(core, "_wait_for_vm_ssh") as readiness:
            result = core.create_gpu_vm(plan["plan_id"])
        self.assertEqual(result["disk_id"], old["disk_id"])
        readiness.assert_called_once_with(result["id"], result["name"], core.SSH_USER)
        self.assertTrue(result["ssh_ready"])
        self.assertFalse(any(args[:3] == ["compute", "disk", "create"] for args in calls))
        self.assertEqual(quota.call_args.kwargs["disk_gib"], 0)
        validator.assert_called_once()
        self.assertFalse(core._pending_launches())
        self.assertFalse(core._reusable_disks())

    def test_validation_failure_precedes_allocation_and_submission_marker(self):
        plan = self.plan()
        with patch.object(core, "project_context", return_value=PROJECT), patch.object(core, "preflight_vm", return_value=GOOD), \
             patch.object(core, "ensure_ssh_key"), patch.object(core, "_cloud_init", return_value=""), \
             patch.object(core, "validate_instance_request", side_effect=core.NebiusError("Invalid JSON enum")), \
             patch.object(core, "run_cli") as cli:
            with self.assertRaisesRegex(core.NebiusError, "Invalid JSON enum"):
                core.create_gpu_vm(plan["plan_id"])
            cli.assert_not_called()
        self.assertFalse(core._pending_launches())
        self.assertEqual(core._read_json(core.OPERATION_FILE, {})["phase"], "error")
        self.assertEqual(core._read_json(core.OPERATION_FILE, {})["stage"], "preflight")
        self.assertNotIn("submitted_at", core._read_json(core.PLAN_DIR / f"{plan['plan_id']}.json", {}))

    def test_validator_requires_network_namespace_and_expected_transport_failure(self):
        with patch.object(core.subprocess, "run", return_value=subprocess.CompletedProcess([], 20, "", "code = Unavailable: network is unreachable")) as run:
            self.assertTrue(core.validate_instance_request({})["valid"])
            self.assertEqual(run.call_args.args[0][:4], ["/usr/bin/unshare", "--user", "--map-root-user", "--net"])
        for detail in ("unshare: Operation not permitted", "Error: read protojson from positional arguments: invalid enum", "unknown profile"):
            with patch.object(core.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "", detail)):
                with self.assertRaises(core.NebiusError):
                    core.validate_instance_request({})

    def test_existing_disk_does_not_require_another_200_gib_quota(self):
        quota = {"metadata": {"name": "compute.disk.size.network-ssd"},
                 "spec": {"region": "eu-west1", "limit": 200 * 1024**3}, "status": {"usage": 200 * 1024**3}}
        with patch.object(core, "run_cli", return_value={"items": [quota]}):
            self.assertFalse(core.preflight_vm("eu-west1")["ready"])
            self.assertTrue(core.preflight_vm("eu-west1", disk_gib=0)["ready"])

    def preflight_cli(self, *, allowed=True, gpu_limit=4, ssd_limit=1000, instances=None):
        def cli(args, **kwargs):
            if args[:3] == ["compute", "platform", "get-by-name"]:
                return {"metadata": {"name": OFFERING["platform"], "parent_id": "project-public-images"},
                        "spec": {"gpu_count_quota_type": "compute.instance.gpu.h200", "presets": [
                            {"name": OFFERING["preset"], "resources": {"gpu_count": 1}}]},
                        "status": {"allowed_for_preemptibles": True} if allowed else {}}
            if args[:2] == ["quotas", "quota-allowance"]:
                return {"items": [{"metadata": {"name": name}, "spec": {"region": "eu-west1", "limit": limit},
                                   "status": {"usage": 0}}
                                  for name, limit in [("compute.disk.size.network-ssd", ssd_limit * 1024**3),
                                                      ("compute.instance.gpu.h200", gpu_limit)]]}
            if args[:3] == ["compute", "image", "get-latest-by-family"]:
                return {"status": {"state": "READY", "min_disk_size_bytes": 40 * 1024**3}}
            if args[:3] == ["vpc", "subnet", "list"]:
                return {"items": [{"metadata": {"id": PROJECT["subnet_id"]}, "status": {"state": "READY"}}]}
            if args[:3] == ["capacity", "resource-advice", "list"]:
                return {"items": [{"spec": {"region": "eu-west1", "compute_instance": {
                    "platform": OFFERING["platform"], "preset": {"name": OFFERING["preset"]}}},
                    "status": {"on_demand": {"available": 3}, "preemptible": {"available": 3}}}]}
            if args[:3] == ["compute", "instance", "list"]:
                return {"items": instances or []}
            if args[:3] == ["compute", "disk", "list"]:
                return {}
            self.fail(f"Unexpected CLI call (mutations forbidden): {args}")
        return cli

    def full_preflight(self, allocation="preemptible", **kwargs):
        return core.preflight_vm("eu-west1", allocation, OFFERING["platform"], 1,
                                 project_id=PROJECT["project_id"], preset=OFFERING["preset"],
                                 subnet_id=PROJECT["subnet_id"], vm_name="new-vm", image_family=core.IMAGE_FAMILY, **kwargs)

    def test_preemptible_default_false_blocks_before_any_cloud_mutation(self):
        with patch.object(core, "run_cli", side_effect=self.preflight_cli(allowed=False)) as cli:
            result = self.full_preflight()
        self.assertFalse(result["ready"])
        self.assertIn("does not allow preemptible", result["message"])
        self.assertTrue(all(call.args[0][2] in {"get-by-name", "get-latest-by-family", "list"} for call in cli.call_args_list))

    def test_compatible_preflight_passes_and_on_demand_gpu_quota_blocks(self):
        with patch.object(core, "run_cli", side_effect=self.preflight_cli()):
            self.assertTrue(self.full_preflight()["ready"])
            self.assertTrue(self.full_preflight("on_demand")["ready"])
        with patch.object(core, "run_cli", side_effect=self.preflight_cli(gpu_limit=0)):
            self.assertFalse(self.full_preflight("on_demand")["ready"])
            # Regular PAYG GPU quota must not be misapplied to preemptibles.
            self.assertTrue(self.full_preflight("preemptible")["ready"])

    def test_preflight_checks_names_subnet_preset_image_and_capacity(self):
        for command, replacement, label in [
            (["compute", "instance", "list"], {"items": [{"metadata": {"name": "new-vm"}}]}, "Unique instance name"),
            (["compute", "disk", "list"], {"items": [{"metadata": {"name": "new-vm-boot"}}]}, "Unique disk name"),
            (["vpc", "subnet", "list"], {}, "Network"),
            (["compute", "image", "get-latest-by-family"], {"status": {"state": "ERROR"}}, "Boot image"),
            (["compute", "platform", "get-by-name"], {"metadata": {"name": OFFERING["platform"]}, "status": {"allowed_for_preemptibles": True}}, "GPU configuration"),
        ]:
            with self.subTest(label=label):
                normal = self.preflight_cli()
                with patch.object(core, "run_cli", side_effect=lambda args, **kw: replacement if args[:3] == command else normal(args, **kw)):
                    result = self.full_preflight()
                self.assertFalse(result["ready"])
                self.assertTrue(any(row["name"] == label and row["state"] == "blocked" for row in result["checks"]))
        normal = self.preflight_cli()
        def no_capacity(args, **kw):
            value = normal(args, **kw)
            if args[:2] == ["capacity", "resource-advice"]:
                value["items"][0]["status"]["preemptible"] = {"available": 0, "availability_level": "AVAILABILITY_LEVEL_HIGH"}
            return value
        with patch.object(core, "run_cli", side_effect=no_capacity):
            self.assertIn("No capacity", self.full_preflight()["message"])

    def test_unverifiable_critical_preflight_blocks(self):
        for failing in (["compute", "platform"], ["quotas", "quota-allowance"]):
            normal = self.preflight_cli()
            def unavailable(args, **kw):
                if args[:2] == failing:
                    raise core.NebiusError("permission denied or timed out")
                return normal(args, **kw)
            with patch.object(core, "run_cli", side_effect=unavailable):
                self.assertFalse(self.full_preflight()["ready"])

    def test_live_recheck_rejects_preemptibles_before_key_disk_or_pending(self):
        plan = self.plan("preemptible")
        with patch.object(core, "project_context", return_value=PROJECT), \
             patch.object(core, "run_cli", side_effect=self.preflight_cli(allowed=False)), \
             patch.object(core, "ensure_ssh_key") as key:
            with self.assertRaisesRegex(core.NebiusError, "does not allow preemptible"):
                core.create_gpu_vm(plan["plan_id"])
            key.assert_not_called()
        self.assertFalse(core._pending_launches())

    def test_specific_admission_rejection_can_be_repaired_but_other_projects_cannot(self):
        error = (f"Nebius: Error: rpc error: code = InvalidArgument desc = Preemptible is invalid\n"
                 f"Preemptible: preemptible instances of platform {OFFERING['platform']} are not allowed in {PROJECT['project_id']}\n")
        plan, disk = self.rejected_launch(error)
        with patch.object(core, "run_cli", side_effect=[disk, {}]):
            self.assertEqual(len(core.repair_rejected_launches()["resolved"]), 1)
        self.assertFalse(core._pending_launches())
        self.assertFalse(core._known_create_rejection(plan, error.replace(PROJECT["project_id"], "project-someone-else")))

    def saved_disk(self):
        plan, disk = self.rejected_launch()
        with patch.object(core, "run_cli", side_effect=[disk, {}]):
            core.repair_rejected_launches()
        return plan, disk

    def test_disk_cleanup_requires_confirmation_and_live_unattached_ownership(self):
        plan, disk = self.saved_disk()
        with patch.object(core, "run_cli") as cli:
            with self.assertRaisesRegex(core.NebiusError, "confirmation"):
                core.delete_saved_disk(plan["disk_id"])
            cli.assert_not_called()
        with patch.object(core, "sync_personal_projects", return_value={"projects": [PROJECT]}), \
             patch.object(core, "run_cli", side_effect=[disk, {}, {"id": "operation-delete"}, {"status": {}}]) as cli:
            self.assertTrue(core.delete_saved_disk(plan["disk_id"], True)["deleted"])
            self.assertIn(["compute", "disk", "delete", plan["disk_id"], "--async", "--format", "json"], [c.args[0] for c in cli.call_args_list])
        self.assertFalse(core._reusable_disks())

    def test_cleanup_never_deletes_attached_reconciling_protected_or_wrongly_owned_disk(self):
        plan, disk = self.saved_disk()
        for change in [
            {"status": {**disk["status"], "read_write_attachment": "computeinstance-stopped"}},
            {"status": {**disk["status"], "read_only_attachments": ["computeinstance-stopped"]}},
            {"status": {**disk["status"], "reconciling": True}},
            {"spec": {"forbid_deletion": True}},
            {"metadata": {**disk["metadata"], "labels": {}}},
        ]:
            with patch.object(core, "sync_personal_projects", return_value={"projects": [PROJECT]}), \
                 patch.object(core, "run_cli", side_effect=[{**disk, **change}, {}]) as cli:
                with self.assertRaises(core.NebiusError):
                    core.delete_saved_disk(plan["disk_id"], True)
                self.assertFalse(any(call.args[0][2] == "delete" for call in cli.call_args_list))
        self.assertEqual(len(core._reusable_disks()), 1)

    def test_old_timer_plan_cannot_silently_create_without_a_timer(self):
        plan = self.plan()
        plan["auto_stop_hours"] = 2
        core._atomic_json(core.PLAN_DIR / f"{plan['plan_id']}.json", plan)
        with patch.object(core, "run_cli") as cli:
            with self.assertRaisesRegex(core.NebiusError, "old plan included auto-stop"):
                core.create_gpu_vm(plan["plan_id"])
            cli.assert_not_called()
        with self.assertRaisesRegex(core.NebiusError, "Auto-stop has been removed"):
            core.plan_gpu_vm(auto_stop_hours=2)

    def test_mcp_no_longer_exposes_auto_stop(self):
        tools = {tool["name"]: tool for tool in mcp.TOOLS}
        self.assertNotIn("set_auto_stop", tools)
        self.assertNotIn("auto_stop_hours", tools["plan_gpu_vm"]["inputSchema"]["properties"])
        with self.assertRaisesRegex(core.NebiusError, "Unknown tool"):
            mcp._call("set_auto_stop", {"vm_id": "computeinstance-test", "hours": 2, "confirmed": True})

    def test_delete_vm_keeps_disk_if_still_attached_after_vm_deletion(self):
        plan, disk = self.rejected_launch()
        vm = {"id": "computeinstance-test", "name": "test", "disk_id": plan["disk_id"],
              "project_id": PROJECT["project_id"], "source_request_id": plan["plan_id"]}
        core._save_registry({"vms": [vm]})
        instance = {"spec": {"boot_disk": {"existing_disk": {"id": plan["disk_id"]}}}}
        disk["status"]["read_write_attachment"] = "computeinstance-other"
        with patch.object(core, "_verify_deletion_owner", return_value={"tenant_id": "tenant-test", "subject_id": "tenantuseraccount-test"}), patch.object(core, "_accessible_vm", return_value=(instance, vm)), \
             patch.object(core, "run_cli", side_effect=[disk, {"id": "operation-delete"}, {"status": {}}, disk]) as cli:
            with self.assertRaisesRegex(core.NebiusError, "not safe to delete"):
                core.delete_vm(vm["id"], True)
        self.assertTrue(core._registered(vm["id"])["instance_deleted"])
        self.assertEqual([call.args[0][:3] for call in cli.call_args_list],
                         [["compute", "disk", "get"], ["compute", "instance", "delete"], ["compute", "instance", "operation"], ["compute", "disk", "get"]])

    def test_delete_vm_deletes_only_confirmed_boot_disk_after_live_checks(self):
        plan, disk = self.rejected_launch()
        vm = {"id": "computeinstance-test", "name": "test", "disk_id": plan["disk_id"],
              "project_id": PROJECT["project_id"], "source_request_id": plan["plan_id"]}
        core._save_registry({"vms": [vm]})
        instance = {"spec": {"boot_disk": {"existing_disk": {"id": plan["disk_id"]}},
                             "secondary_disks": [{"existing_disk": {"id": "computedisk-preserved"}}]}}
        with patch.object(core, "_verify_deletion_owner", return_value={"tenant_id": "tenant-test", "subject_id": "tenantuseraccount-test"}), patch.object(core, "_accessible_vm", return_value=(instance, vm)), \
             patch.object(core, "run_cli", side_effect=[disk, {"id": "operation-vm"}, {"status": {}}, disk, {}, {"id": "operation-disk"}, {"status": {}}]) as cli, \
             patch.object(core, "HOME", core.STATE_DIR), patch.object(core.subprocess, "run"):
            result = core.delete_vm(vm["id"], True)
        self.assertTrue(result["disk_deleted"])
        self.assertFalse(core._registry()["vms"])
        self.assertIn(["compute", "disk", "delete", plan["disk_id"], "--async", "--format", "json"], [c.args[0] for c in cli.call_args_list])
        self.assertNotIn("computedisk-preserved", str(cli.call_args_list))


if __name__ == "__main__":
    unittest.main()
