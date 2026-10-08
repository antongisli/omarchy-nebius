"""Reusable project/platform spot policies and live calculator estimates.

Contracts: https://github.com/nebius/api/tree/main/nebius/billing
Planning is read-only. A missing default is created only by a confirmed write.
"""

import copy
import datetime as dt
from decimal import Decimal, InvalidOperation
import functools
import hashlib
import json
import re
import secrets

import nebius_core as core

PRICING_URL = "https://docs.nebius.com/signup-billing/pricing-policy"
CONSOLE_URL = "https://console.nebius.com/"


def default_max_price(platform):
    """Published PAYG GPU rate minus one cent; CPU, RAM and disks stay separate."""
    payg = core.ON_DEMAND_GPU_USD.get(platform)
    if payg is None:
        raise core.NebiusError("No published PAYG rate is available for this GPU. Select a saved cap or Follow spot price")
    return amount(format(Decimal(str(payg)) - Decimal("0.01"), ".3f"))


def amount(value):
    """Keep monetary input exact; the API accepts at most three decimals."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,8}(?:\.\d{1,3})?", value):
        raise core.NebiusError("Enter a positive price with up to three decimal places, for example 5.000")
    if Decimal(value) <= 0:
        raise core.NebiusError("The maximum price must be greater than zero")
    return format(Decimal(value), ".3f")


def policy_name(value):
    if not isinstance(value, str) or not value.strip() or not value.isprintable() or len(value.encode("utf-8")) > 1024:
        raise core.NebiusError("Enter a policy name using printable characters, up to 1024 UTF-8 bytes")
    return value.strip()


@functools.lru_cache(maxsize=4)
def _check_cli(cli):
    try:
        text = core._run([cli, "--no-check-update", "billing", "pricing-policy", "create", "--help"], timeout=15)
        if "--pricing-max-price-v1-max-price" not in text:
            raise core.NebiusError("Pricing-policy commands are missing")
    except core.NebiusError as error:
        raise core.NebiusError("Spot pricing requires the updated Nebius CLI. Run Set up / reconnect, then reopen the plugin.") from error


def require_support():
    _check_cli(str(core.CLI))


def _scope(project_id):
    if project_id not in {p["project_id"] for p in core.sync_personal_projects()["projects"]}:
        raise core.NebiusError("Choose a personal project for pricing policies")


def policy_row(resource):
    metadata, spec, status = (resource.get(k, {}) for k in ("metadata", "spec", "status"))
    currency = status.get("currency")
    currency = currency.strip().upper() if isinstance(currency, str) else ""
    try:
        maximum = amount(spec["pricing"]["max_price_v1"]["max_price"])
        platform = spec["compute_instance_spec"]["v1"]["platform"]
    except (KeyError, TypeError) as error:
        raise core.NebiusError("Nebius returned an unsupported pricing policy") from error
    return {"id": metadata.get("id", ""), "name": metadata.get("name", ""),
            "project_id": metadata.get("parent_id", ""), "platform": platform,
            "max_price": maximum, "currency": currency or "UNKNOWN",
            "resource_version": str(metadata.get("resource_version", "")),
            "state": status.get("state", "STATE_UNSPECIFIED"),
            "scheduling_state": status.get("scheduling_state", "SCHEDULING_STATE_UNSPECIFIED"),
            "running_vm_count": int(status.get("running_vm_count", 0)),
            "labels": metadata.get("labels") or {}}


def matches_policy_identity(row, name, labels):
    """Allow absent labels on an exact name; never ignore conflicting labels.

    Reuse still requires scope and price checks. This does not establish
    ownership for updating or deleting the policy.
    """
    expected = {**labels, "managed-by": core.MANAGED_BY}
    actual = row["labels"]
    return (all(actual[key] == value for key, value in expected.items() if key in actual) and
            (row["name"] == name or all(actual.get(key) == value for key, value in expected.items())))


def verify_cap(row, maximum, currency="USD"):
    if row["max_price"] == maximum and row["currency"] == currency:
        return
    expected = f"{currency} {maximum}/GPU-hour"
    if row["currency"] == "UNKNOWN":
        raise core.NebiusError("Nebius did not report the pricing policy's currency. "
                              f"Expected cap: {expected}; cloud amount: {row['max_price']}/GPU-hour. "
                              "Refresh pricing or check the policy in Nebius console")
    raise core.NebiusError("The cloud policy differs from your saved cap. "
                          f"Expected: {expected}; cloud: {row['currency']} {row['max_price']}/GPU-hour. "
                          "Review it in Nebius console or select another policy")


def get_policy(policy_id, project_id=None, platform=None):
    require_support()
    if not re.fullmatch(r"pricingpolicy-[A-Za-z0-9_-]+", str(policy_id)):
        raise core.NebiusError("Select a valid pricing policy")
    row = policy_row(core.run_cli(["billing", "pricing-policy", "get", "--id", policy_id, "--format", "json"]))
    if project_id and row["project_id"] != project_id:
        raise core.NebiusError("The pricing policy belongs to another project. Select a compatible policy")
    if platform and row["platform"] != platform:
        raise core.NebiusError("The pricing policy belongs to another GPU platform. Select a compatible policy")
    return row


def _preference_key(project_id, platform):
    return project_id + "/" + platform


def default_name(platform):
    return "omarchy-spot-" + platform.removeprefix("gpu-")


def _managed_default(rows):
    defaults = [p for p in rows if p["labels"].get("managed-by") == core.MANAGED_BY
                and p["labels"].get("pricing-default") == "true"]
    if len(defaults) > 1:
        raise core.NebiusError("Several default policies exist. Select one and save it as the default")
    return defaults[0] if defaults else None


def list_policies(project_id, platform=""):
    _scope(project_id)
    require_support()
    rows = [policy_row(item) for item in core._items(core.run_cli(
        ["billing", "pricing-policy", "list", "--parent-id", project_id, "--all", "--format", "json"]))]
    rows = [row for row in rows if row["project_id"] == project_id and (not platform or row["platform"] == platform)]
    saved = core._read_json(core.STATE_DIR / "pricing-preferences.json", {})
    preferred = saved.get(_preference_key(project_id, platform), "")
    return {"policies": sorted(rows, key=lambda p: (p["name"], p["id"])), "default_policy_id": preferred,
            "default_max_price": default_max_price(platform) if platform in core.ON_DEMAND_GPU_USD else None,
            "default_currency": "USD",
            "range_note": "Allowed ranges are shown in Nebius console: Billing → Pricing. Limits outside the range are rejected."}


def select_default(project_id, policy_id):
    _scope(project_id)
    row = get_policy(policy_id, project_id)
    with core.mutation_guard(resource="pricing-preferences"):
        path = core.STATE_DIR / "pricing-preferences.json"
        saved = core._read_json(path, {})
        saved[_preference_key(project_id, row["platform"])] = policy_id
        core._atomic_json(path, saved)
    return row


def list_platforms(project_id):
    """Policy management must work without a VM, free capacity or a subnet."""
    saved = {row["platform"] for row in list_policies(project_id)["policies"]}
    warnings = []
    try:
        resources = core._items(core.run_cli(["compute", "platform", "list", "--parent-id", project_id,
                                             "--all", "--format", "json"]))
        available = {row.get("metadata", {}).get("name", "") for row in resources
                     if row.get("status", {}).get("allowed_for_preemptibles") is not False}
        available = {name for name in available if name.startswith("gpu-")}
    except core.NebiusError as error:
        if not saved:
            raise
        available = set()
        warnings.append("Could not refresh GPU platforms; showing platforms with saved policies. " + str(error))
    return {"platforms": sorted(saved | available), "warnings": warnings}


def resolve(project_id, platform, mode="default", policy_id=""):
    if mode not in {"default", "policy", "follow"}:
        raise core.NebiusError("Choose default, policy, or follow spot pricing")
    if mode != "policy" and policy_id:
        raise core.NebiusError("A policy ID can only be used with policy pricing")
    if mode == "follow":
        require_support()
        return {"mode": "follow", "policy": None, "create_default": False}
    if mode == "policy":
        row = get_policy(policy_id, project_id, platform)
    else:
        result = list_policies(project_id, platform)
        if result["default_policy_id"]:
            # A deleted/revoked preference must not silently turn into a different cap.
            row = get_policy(result["default_policy_id"], project_id, platform)
        else:
            named = [p for p in result["policies"] if p["name"] == default_name(platform)]
            managed = _managed_default(result["policies"])
            if managed:
                row = managed  # Labels survive renames and work on another installation.
            elif named:
                raise core.NebiusError("The default policy name is already in use. Select an existing policy or create a differently named one")
            else:
                row = {"id": "", "name": default_name(platform), "project_id": project_id, "platform": platform,
                       "max_price": default_max_price(platform), "currency": "USD", "resource_version": "",
                       "state": "PLANNED", "scheduling_state": "PENDING_CREATION", "running_vm_count": 0}
    return {"mode": "policy", "policy": row, "create_default": not bool(row["id"])}


def terms(pricing):
    row = pricing.get("policy") or {}
    return {"mode": pricing["mode"], **{k: row.get(k) for k in
            ("id", "project_id", "platform", "max_price", "currency", "resource_version")}}


def check(pricing):
    if pricing["mode"] == "follow":
        return {"name": "Spot pricing", "state": "ok", "message": "Follows the changing spot price; no user-set maximum"}
    row = pricing["policy"]
    if pricing.get("create_default"):
        return {"name": "Spot pricing", "state": "ok", "message":
                f"Apply {row['currency']} {row['max_price']}/GPU-hour cap on confirmation; Nebius must accept its range before any disk or VM is allocated"}
    allowed = row["state"] == "STATE_ACTIVE" and row["scheduling_state"] == "SCHEDULING_STATE_ALLOWED"
    if row["currency"] == "UNKNOWN":
        allowed = False
    message = ("Policy permits scheduling; capacity is checked separately" if allowed else
               "Preemptible launch blocked by the price limit" if row["scheduling_state"] == "SCHEDULING_STATE_BLOCKED" else
               "Policy is not ready or its currency is unknown; refresh or select another policy")
    return {"name": "Spot pricing", "state": "ok" if allowed else "blocked", "message": message}


def add_preflight(preflight, pricing):
    value = copy.deepcopy(preflight)
    item = check(pricing)
    value["checks"].append(item)
    if item["state"] != "ok":
        value.update(ready=False, message=item["message"], recovery="Wait for the spot price to fall, edit the policy, or select another pricing option")
    return value


def _policy_error(error):
    if "outofrange" in str(error).replace("_", "").replace(" ", "").lower():
        return core.NebiusError("The maximum price is outside this platform's allowed range. The limit was not adjusted. "
                                "Open Settings → Spot pricing policies and enter an allowed value.\n"
                                "Check the allowed range in Nebius console → Billing → Pricing. "
                                "No new disk or VM was allocated.\n" + str(error))
    return error


def create_policy(project_id, platform, name, max_price, *, default=False, labels=None):
    _scope(project_id)
    require_support()
    maximum = amount(max_price)
    name = policy_name(name)
    if not re.fullmatch(r"gpu-[a-z0-9-]+", platform):
        raise core.NebiusError("Select a GPU platform")
    with core.mutation_guard(resource="pricing-" + project_id):
        existing = list_policies(project_id, platform)["policies"]
        found = (_managed_default(existing) if default else None) or next((p for p in existing if p["name"] == name), None)
        if found:
            found = get_policy(found["id"], project_id, platform)
            verify_cap(found, maximum)
            if ((labels and not matches_policy_identity(found, name, labels)) or
                    (default and (found["labels"].get("pricing-default") != "true" or
                                  found["labels"].get("managed-by") != core.MANAGED_BY))):
                raise core.NebiusError("That policy already exists with different terms. Review and select it instead")
            return found
        labels = {**(labels or {}), "managed-by": core.MANAGED_BY}
        if default:
            labels["pricing-default"] = "true"
        request = {"metadata": {"parent_id": project_id, "name": name, "labels": labels},
                   "spec": {"compute_instance_spec": {"v1": {"platform": platform}},
                            "pricing": {"max_price_v1": {"max_price": maximum}}}}
        try:
            core.run_cli(["billing", "pricing-policy", "create", json.dumps(request), "--retries", "1", "--format", "json"], timeout=180)
        except core.NebiusError as error:
            # Never replay an uncertain write. The unique name makes a later retry reconcilable.
            raise _policy_error(error)
        row = policy_row(core.run_cli(["billing", "pricing-policy", "get-by-name", "--parent-id", project_id,
                                      "--name", name, "--format", "json"]))
        if row["project_id"] != project_id or row["platform"] != platform:
            raise core.NebiusError("Created policy belongs to a different project or GPU platform. Review the policy before launching")
        verify_cap(row, maximum)
        return row


def update_policy(policy_id, name, max_price, expected_version):
    row = get_policy(policy_id)
    _scope(row["project_id"])
    maximum = amount(max_price)
    if row["resource_version"] != str(expected_version) or not expected_version:
        raise core.NebiusError("The policy changed since review. Refresh and review its new terms")
    if row["currency"] != "USD":
        raise core.NebiusError("The public policy editor accepts USD limits. Edit this policy in Nebius console")
    if maximum != row["max_price"] and row["running_vm_count"]:
        raise core.NebiusError("Stop all VMs using this policy before changing its limit, or create a separate policy")
    with core.mutation_guard(resource="pricing-" + row["project_id"]):
        try:
            core.run_cli(["billing", "pricing-policy", "update", policy_id, "--name", policy_name(name),
                          *(["--pricing-max-price-v1-max-price", maximum] if maximum != row["max_price"] else []),
                          "--resource-version", str(expected_version),
                          "--retries", "1", "--format", "json"], timeout=180)
        except core.NebiusError as error:
            raise _policy_error(error)
    return get_policy(policy_id, row["project_id"], row["platform"])


def refresh_terms(pricing, project_id, platform, *, create=False):
    if pricing.get("global_policy"):
        import nebius_global_pricing as global_pricing
        return global_pricing.refresh_terms(pricing, project_id, platform, create=create)
    if pricing["mode"] == "follow":
        require_support()
        return pricing
    previous = pricing["policy"]
    if pricing.get("create_default"):
        if not create:
            return pricing
        row = create_policy(project_id, platform, previous["name"], previous["max_price"], default=True)
    else:
        row = get_policy(previous["id"], project_id, platform)
        if terms({"mode": "policy", "policy": row}) != terms(pricing):
            raise core.NebiusError("The pricing policy changed since review. Review a fresh plan before continuing")
    result = {"mode": "policy", "policy": row, "create_default": False}
    state = check(result)
    if state["state"] != "ok":
        raise core.NebiusError(state["message"] + ". No new disk or VM was allocated. Review Spot pricing")
    return result


def request_fields(pricing):
    if pricing["mode"] == "follow":
        return {"follows_spot_price": {}}
    return {"spot_pricing_policy": {"id": pricing["policy"]["id"] or "pricingpolicy-validation"}}


def instance_pricing(instance):
    spec = instance.get("spec", {})
    if not spec.get("preemptible"):
        return {"mode": "on_demand"}
    if spec.get("spot_pricing_policy", {}).get("id"):
        return {"mode": "policy", "policy_id": spec["spot_pricing_policy"]["id"]}
    if "follows_spot_price" in spec:
        return {"mode": "follow"}
    return {"mode": "legacy", "note": "Choose a pricing policy before restarting this older preemptible VM"}


def vm_details(vm_id):
    instance, vm = core._accessible_vm(vm_id)
    choice = instance_pricing(instance)
    if choice["mode"] in {"on_demand", "legacy"}:
        return {"vm": vm, "pricing": choice, "ready": choice["mode"] == "on_demand"}
    try:
        pricing = resolve(vm["project_id"], vm["platform"], choice["mode"], choice.get("policy_id", ""))
    except core.NebiusError as error:
        # Keep policy replacement available even after its old policy was deleted.
        return {"vm": vm, "pricing": choice, "ready": False, "pricing_error": str(error),
                "check": {"name": "Spot pricing", "state": "blocked", "message": str(error)}}
    if pricing.get("policy"):
        import nebius_global_pricing as global_pricing
        pricing["policy"]["display_name"] = global_pricing.display_name(pricing["policy"])
    result = {"vm": vm, "pricing": pricing, "ready": check(pricing)["state"] == "ok", "check": check(pricing)}
    count = re.match(r"^(\d+)gpu(?:-|$)", str(vm.get("preset", "")))
    result["gpu_count"] = int(count[1]) if count else None
    try:
        spec = instance["spec"]
        disk = core.run_cli(["compute", "disk", "get", spec["boot_disk"]["existing_disk"]["id"], "--format", "json"])
        if not count or not disk.get("status", {}).get("size_bytes"):
            raise ValueError("VM size is not available")
        quote_plan = {"project": {"project_id": vm["project_id"]}, "plan_id": "pricing-review", "name": vm["name"],
                      "platform": vm["platform"], "preset": vm["preset"], "gpu_count": int(count[1]),
                      "allocation": "preemptible", "spot_pricing": pricing,
                      "disk_gib": int(disk["status"]["size_bytes"]) // 1024**3,
                      "subnet_id": spec["network_interfaces"][0]["subnet_id"]}
        result.update(price_estimate=estimate(quote_plan, refresh=True), gpu_count=int(count[1]))
    except (core.NebiusError, KeyError, IndexError, TypeError, ValueError) as error:
        result["price_estimate"] = {"state": "unavailable", "error": str(error)}
    return result


def review_start(vm_id):
    result = vm_details(vm_id)
    if not result["ready"]:
        return result
    if result["pricing"]["mode"] == "on_demand":
        return result
    review_id = secrets.token_urlsafe(18)
    saved = {"schema": "nebius.omarchy-start/v1", "vm_id": vm_id, "pricing": result["pricing"],
             "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat()}
    core._atomic_json(core.PLAN_DIR / (review_id + ".json"), saved)
    return {**result, "pricing_review_id": review_id}


def validate_start(instance, vm, review_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", review_id):
        raise core.NebiusError("Review current Spot pricing before starting this VM")
    saved = core._read_json(core.PLAN_DIR / (review_id + ".json"), {})
    try:
        valid = (saved["schema"] == "nebius.omarchy-start/v1" and saved["vm_id"] == vm["id"] and
                 dt.datetime.fromisoformat(saved["expires_at"]) > dt.datetime.now(dt.timezone.utc))
    except (KeyError, ValueError, TypeError):
        valid = False
    if not valid:
        raise core.NebiusError("Spot pricing review expired or does not match this VM. Review again")
    choice = instance_pricing(instance)
    previous = saved["pricing"]
    if (choice["mode"] != previous["mode"] or
            choice.get("policy_id") != (previous.get("policy") or {}).get("id")):
        raise core.NebiusError("VM pricing changed since review. Review again")
    refresh_terms(previous, vm["project_id"], vm["platform"])


def plan_configuration(vm_id, mode="default", policy_id=""):
    instance, vm = core._accessible_vm(vm_id)
    core._require_direct_lifecycle(vm)
    if vm["state"] != "stopped" or not instance.get("spec", {}).get("preemptible"):
        raise core.NebiusError("Pricing can only be changed on a stopped preemptible VM")
    import nebius_global_pricing as global_pricing
    pricing = global_pricing.resolve(vm["project_id"], vm["platform"], mode, policy_id)
    review_id = secrets.token_urlsafe(18)
    result = {"schema": "nebius.omarchy-pricing-change/v1", "vm_id": vm_id,
              "resource_version": str(instance["metadata"].get("resource_version", "")), "pricing": pricing,
              "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat()}
    core._atomic_json(core.PLAN_DIR / (review_id + ".json"), result)
    return {**result, "review_id": review_id}


def configure_vm(vm_id, review_id):
    with core.mutation_guard(resource=vm_id):
        instance, vm = core._accessible_vm(vm_id)
        core._require_direct_lifecycle(vm)
        if vm["state"] != "stopped" or not instance.get("spec", {}).get("preemptible"):
            raise core.NebiusError("Pricing can only be changed on a stopped preemptible VM")
        if core._cloud_operation_path(vm_id).exists():
            raise core.NebiusError("Reconcile the pending VM operation in Activity before changing pricing")
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", review_id):
            raise core.NebiusError("Review a pricing change before applying it")
        saved = core._read_json(core.PLAN_DIR / (review_id + ".json"), {})
        try:
            valid = (saved["schema"] == "nebius.omarchy-pricing-change/v1" and saved["vm_id"] == vm_id and
                     saved["resource_version"] == str(instance["metadata"]["resource_version"]) and
                     dt.datetime.fromisoformat(saved["expires_at"]) > dt.datetime.now(dt.timezone.utc))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise core.NebiusError("The VM or pricing review changed. Review the pricing change again")
        pricing = saved["pricing"]
        if pricing.get("global_policy"):
            import nebius_global_pricing as global_pricing
            pricing = global_pricing.refresh_terms(pricing, vm["project_id"], vm["platform"], create=True, require_allowed=False)
        elif pricing.get("create_default"):
            pricing = refresh_terms(pricing, vm["project_id"], vm["platform"], create=True)
        elif pricing["mode"] == "policy":
            row = get_policy(pricing["policy"]["id"], vm["project_id"], vm["platform"])
            if terms({"mode": "policy", "policy": row}) != terms(pricing):
                raise core.NebiusError("The policy changed. Review the pricing change again")
        # Selecting a currently blocked policy is allowed; it still cannot start.
        spec = copy.deepcopy(instance["spec"])
        for key in ("follows_spot_price", "spot_pricing_policy", "on_demand"):
            spec.pop(key, None)
        spec.update(request_fields(pricing))
        metadata = {k: v for k, v in instance["metadata"].items() if k in {"id", "parent_id", "name", "labels", "resource_version"}}
        if not metadata.get("resource_version"):
            raise core.NebiusError("The VM version is unavailable. Refresh before changing pricing")
        core.run_cli(["compute", "instance", "update", json.dumps({"metadata": metadata, "spec": spec}),
                      "--full", "--retries", "1", "--format", "json"], timeout=180)
        return {"id": vm_id, "pricing": pricing, "state": "stopped"}


def estimate(plan, *, refresh=False):
    """Read the public calculator; never substitute a fixed Spot discount.

    v1alpha1 returns USD totals, not a separate market-price/range feed. CPU/RAM
    are included, so dividing an L40S total by GPU count is not a spot unit price.
    """
    request = core._instance_request(plan, "computedisk-validation", cloud_init="")
    if plan["allocation"] == "preemptible":
        request["spec"].pop("spot_pricing_policy", None)
        request["spec"]["follows_spot_price"] = {}
    # Calculator pricing does not need cloud-init or a synthetic security group.
    request["spec"].pop("cloud_init_user_data", None)
    for interface in request["spec"]["network_interfaces"]:
        interface.pop("security_groups", None)
    key = hashlib.sha256(json.dumps({"project": plan["project"]["project_id"], "platform": plan["platform"],
        "preset": plan["preset"], "allocation": plan["allocation"], "disk_gib": plan["disk_gib"]}, sort_keys=True).encode()).hexdigest()
    path = core.STATE_DIR / "pricing-cache" / (key + ".json")
    cached = core._read_json(path, {})
    now = dt.datetime.now(dt.timezone.utc)
    try:
        age = max(0, int((now - dt.datetime.fromisoformat(cached["checked_at"])).total_seconds()))
    except (KeyError, ValueError, TypeError):
        age = None
    if not refresh and age is not None and age < 60:
        return {**cached, "age_seconds": age}
    def calculate(resource):
        result = core.run_cli(["billing", "v1alpha1", "calculator", "estimate", json.dumps({"resource_spec": resource}),
                               "--format", "json"], timeout=30)
        value = Decimal(result["hourly_cost"]["general"]["total"]["cost"])
        if not value.is_finite() or value < 0:
            raise ValueError("Invalid estimate")
        return value
    try:
        compute = calculate({"compute_instance_spec": request})
        disk = calculate({"compute_disk_spec": {"metadata": {"parent_id": plan["project"]["project_id"]},
                              "spec": {"type": "NETWORK_SSD", "size_gibibytes": plan["disk_gib"]}}})
        result = {"state": "current", "currency": "USD", "compute_per_hour": str(compute),
                  "storage_per_hour": str(disk), "total_per_hour": str(compute + disk),
                  "checked_at": now.isoformat(), "age_seconds": 0,
                  "per_gpu_hour": str(compute / plan["gpu_count"]) if not plan["platform"].startswith("gpu-l40s") else None,
                  "price_range": None, "source": "Nebius billing calculator", "url": PRICING_URL}
        core._atomic_json(path, result)
        return result
    except (core.NebiusError, KeyError, TypeError, InvalidOperation, ValueError) as error:
        return {**cached, "state": "stale" if cached else "unavailable", "age_seconds": age,
                "error": str(error), "price_range": None, "url": PRICING_URL}


def review_lines(pricing, quote=None, gpu_count=None):
    row = pricing.get("policy") or {}
    if pricing["mode"] == "follow":
        lines = ["Spot pricing: follow current price; no user-set maximum."]
    else:
        lines = [f"Policy: {row.get('display_name') or row['name']}",
                 f"Maximum: {row['currency']} {row['max_price']}/GPU-hour" + (f" · {gpu_count} GPU(s)" if gpu_count else ""),
                 "The limit applies per GPU, not to total spend. Storage and other charges are separate."]
        if gpu_count:
            lines.insert(2, f"GPU limit for this VM: {row['currency']} {Decimal(row['max_price']) * gpu_count:.3f}/hour, plus other charges.")
    if quote:
        if quote["state"] in {"current", "stale"}:
            lines += [f"{'Saved' if quote['state'] == 'stale' else 'Current'} estimate: USD {quote['compute_per_hour']}/hour compute + "
                      f"USD {quote['storage_per_hour']}/hour storage", "Checked: " + quote["checked_at"]]
            if quote.get("per_gpu_hour") is not None:
                lines += ["Compute estimate per GPU-hour: USD " + quote["per_gpu_hour"]]
        else:
            lines += ["Current estimate unavailable. Check Billing → Pricing in Nebius console before continuing."]
    lines += ["Spot prices can change every 15 minutes. Nebius may stop the VM for capacity or a price above its limit.",
              "Allowed price range: Nebius console → Billing → Pricing. Estimates exclude traffic and taxes."]
    return lines
