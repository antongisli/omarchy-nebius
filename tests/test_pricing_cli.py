"""Optional real CLI parser contracts, confined to an unresolvable test endpoint.

NEBIUS_TEST_PRICING_CLI=/path/to/nebius python3 -m unittest tests.test_pricing_cli
Uses an isolated profile, synthetic token, and RFC 2606 .invalid destination.
No user profile, credentials or cloud endpoint are read by the CLI.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_pricing import resource


@unittest.skipUnless(os.environ.get("NEBIUS_TEST_PRICING_CLI"), "Opt-in: requires Nebius CLI 0.12.287+")
class PricingCLIContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        token = root / "token"
        token.write_text("offline-synthetic-token")
        config = root / "config.yaml"
        config.write_text("default: offline\nprofiles:\n  offline:\n    endpoint: offline.invalid:443\n    token-file: " + json.dumps(str(token)) + "\n")
        self.environment = {**{k: v for k, v in os.environ.items() if not k.startswith("NEBIUS_")},
                            "HOME": str(root), "XDG_CONFIG_HOME": str(root)}
        self.base = [os.environ["NEBIUS_TEST_PRICING_CLI"], "--config", str(config), "--profile", "offline",
                     "--no-browser", "--no-check-update", "--no-progress", "--auth-timeout", "1s", "--timeout", "1s", "--retries", "1"]

    def run_request(self, args):
        return subprocess.run(self.base + args, env=self.environment, capture_output=True, text=True, timeout=8)

    def accepted(self, args):
        result = self.run_request(args)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("code = Unavailable", result.stderr)
        self.assertIn("offline.invalid", result.stderr)
        self.assertNotIn("read protojson", result.stderr)

    def test_policy_create_and_update_contracts(self):
        request = resource()
        request.pop("status")
        request["metadata"].pop("id")
        self.accepted(["billing", "pricing-policy", "create", json.dumps(request)])
        self.accepted(["billing", "pricing-policy", "update", "pricingpolicy-example", "--name", "changed",
                       "--pricing-max-price-v1-max-price", "4.000", "--resource-version", "1"])

    def test_spot_instance_choices_and_calculator_contract(self):
        for fields in ({"follows_spot_price": {}}, {"spot_pricing_policy": {"id": "pricingpolicy-example"}}):
            request = {"metadata": {"parent_id": "project-example", "name": "example"},
                       "spec": {"preemptible": {"on_preemption": "STOP"}, "recovery_policy": "FAIL",
                                "resources": {"platform": "gpu-h200-sxm", "preset": "1gpu-16vcpu-200gb"},
                                "boot_disk": {"attach_mode": "READ_WRITE", "existing_disk": {"id": "computedisk-example"}},
                                "network_interfaces": [{"name": "eth0", "subnet_id": "vpcsubnet-example", "ip_address": {}}], **fields}}
            self.accepted(["compute", "instance", "create", json.dumps(request)])
            self.accepted(["billing", "v1alpha1", "calculator", "estimate", json.dumps({"resource_spec": {"compute_instance_spec": request}})])
            request["metadata"].update(id="computeinstance-example", resource_version="1")
            request["spec"]["stopped"] = True
            self.accepted(["compute", "instance", "update", json.dumps(request), "--full"])
        disk = {"metadata": {"parent_id": "project-example"}, "spec": {"type": "NETWORK_SSD", "size_gibibytes": 200}}
        self.accepted(["billing", "v1alpha1", "calculator", "estimate", json.dumps({"resource_spec": {"compute_disk_spec": disk}})])

    def test_negative_control_rejects_unknown_field_before_transport(self):
        result = self.run_request(["billing", "pricing-policy", "create", json.dumps({"not_a_field": True})])
        self.assertIn("unknown field", result.stderr)
        self.assertNotIn("code = Unavailable", result.stderr)
