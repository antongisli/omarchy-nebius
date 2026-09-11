"""Opt-in parser contract tests using the real CLI, never a reachable cloud.

On Omarchy: NEBIUS_TEST_OFFLINE_CLI=1 python3 -m unittest discover -s tests
Every invocation is confined to an unprivileged user/network namespace.
"""

import copy
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import nebius_core as core


@unittest.skipUnless(os.environ.get("NEBIUS_TEST_OFFLINE_CLI") == "1", "Opt-in: requires actual Nebius CLI and Linux unshare")
class RealCLIParserTests(unittest.TestCase):
    def request(self, allocation):
        return core._instance_request({
            "project": {"project_id": "project-validation"}, "plan_id": "a" * 24,
            "name": "offline-validation", "platform": "gpu-h200-sxm",
            "preset": "1gpu-16vcpu-200gb", "subnet_id": "vpcsubnet-validation", "allocation": allocation,
        }, "computedisk-validation", cloud_init="#cloud-config\n")

    def test_real_cli_accepts_both_allocations(self):
        for allocation in ("on_demand", "preemptible"):
            with self.subTest(allocation=allocation):
                self.assertTrue(core.validate_instance_request(self.request(allocation))["valid"])

    def test_real_cli_rejects_each_original_lowercase_enum(self):
        for allocation, path, old in [
            ("on_demand", ("boot_disk", "attach_mode"), "read_write"),
            ("on_demand", ("reservation_policy", "policy"), "forbid"),
            ("preemptible", ("preemptible", "on_preemption"), "stop"),
            ("preemptible", ("recovery_policy",), "fail"),
        ]:
            request = copy.deepcopy(self.request(allocation))
            value = request["spec"]
            for key in path[:-1]:
                value = value[key]
            value[path[-1]] = old
            with self.subTest(field=path), self.assertRaisesRegex(core.NebiusError, "read protojson"):
                core.validate_instance_request(request)

    def test_real_cli_rejects_unknown_fields(self):
        request = self.request("on_demand")
        request["spec"]["not_a_real_field"] = True
        with self.assertRaisesRegex(core.NebiusError, "unknown field"):
            core.validate_instance_request(request)
