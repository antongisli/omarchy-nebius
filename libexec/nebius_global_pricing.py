"""Installation-wide Spot caps, materialized in the launch project's platform.

Saved revisions are immutable in the cloud. Editing a cap changes future
selections, while existing VMs keep the terms that were reviewed for them.
"""

import copy
from decimal import Decimal
import re
import secrets

import nebius_catalog as catalog
import nebius_core as core
import nebius_pricing as pricing


SCHEMA = "nebius.omarchy-global-pricing/v2"
LEGACY_SCHEMA = "nebius.omarchy-global-pricing/v1"
ID_LABEL = "pricing-global-id"
VERSION_LABEL = "pricing-global-version"


def _initial():
    policies, defaults = {}, {}
    for platform in core.ON_DEMAND_GPU_USD:
        gpu = catalog.gpu_name(platform)
        policy_id = "spotpolicy-payg" + re.sub(r"[^a-z0-9]", "", gpu.lower())
        maximum = pricing.default_max_price(platform)
        if policy_id in policies and policies[policy_id]["max_price"] != maximum:
            raise core.NebiusError("GPU variants have different PAYG rates; separate their default policies")
        policies[policy_id] = {"id": policy_id, "name": gpu + " default", "max_price": maximum,
                               "currency": "USD", "resource_version": "1"}
        defaults[platform] = policy_id
    return {"schema": SCHEMA, "default_policy_ids": defaults, "policies": list(policies.values())}


def _load():
    path = core.STATE_DIR / "global-pricing.json"
    value = core._read_json(path, None)
    if value is None and not path.exists():
        return _initial()
    try:
        if value["schema"] not in {SCHEMA, LEGACY_SCHEMA}:
            raise ValueError("Unsupported schema")
        rows = value["policies"]
        if not isinstance(rows, list) or not rows:
            raise ValueError("Missing policies")
        ids = set()
        for row in rows:
            if not re.fullmatch(r"spotpolicy-[a-z0-9]+", row["id"]) or row["id"] in ids:
                raise ValueError("Invalid policy ID")
            ids.add(row["id"])
            pricing.policy_name(row["name"])
            if (row["max_price"] != pricing.amount(row["max_price"]) or row["currency"] != "USD" or
                    not re.fullmatch(r"[1-9][0-9]*", row["resource_version"])):
                raise ValueError("Invalid policy terms")
        if value["schema"] == LEGACY_SCHEMA:
            if not isinstance(value["default_policy_id"], str) or (value["default_policy_id"] or "spotpolicy-default") not in ids:
                raise ValueError("Missing default")
            # Replace the old one-size default for future selections only. Retain
            # custom caps for explicit selection; never update cloud policies.
            initial = _initial()
            stock = {"id": "spotpolicy-default", "name": "Default cap", "max_price": "5.000",
                     "currency": "USD", "resource_version": "1"}
            initial["policies"] += [row for row in rows if row != stock]
            if len({row["id"] for row in initial["policies"]}) != len(initial["policies"]):
                raise ValueError("Duplicate migrated policy")
            value = initial
        else:
            defaults = value["default_policy_ids"]
            if (not isinstance(defaults, dict) or not set(core.ON_DEMAND_GPU_USD).issubset(defaults) or
                    any(not re.fullmatch(r"gpu-[a-z0-9-]+", platform) or policy_id not in ids
                        for platform, policy_id in defaults.items())):
                raise ValueError("Missing GPU default")
    except (ValueError, KeyError, TypeError, core.NebiusError) as error:
        raise core.NebiusError("Saved global pricing is unreadable. Restore global-pricing.json before choosing a cap") from error
    return value


def _save(value):
    core._atomic_json(core.STATE_DIR / "global-pricing.json", value)


def _find(value, policy_id):
    for row in value["policies"]:
        if row["id"] == policy_id:
            return copy.deepcopy(row)
    raise core.NebiusError("The saved global policy is missing. Select another policy in Settings")


def list_policies():
    value = _load()
    defaults = value["default_policy_ids"]
    policies = [{**row, "default_gpus": sorted({catalog.gpu_name(platform)
                 for platform, policy_id in defaults.items() if row["id"] == policy_id})}
                for row in value["policies"]]
    return {"policies": sorted(policies, key=lambda row: (row["name"], row["id"])),
            "default_policy_ids": defaults, "default_currency": "USD",
            "platform_defaults": [{"platform": platform, "gpu": catalog.gpu_name(platform), "policy_id": policy_id}
                                  for platform, policy_id in sorted(defaults.items())],
            "pricing_url": core.PRICING_URL, "pricing_checked_at": core.PRICING_CHECKED_AT,
            "range_note": "Initial caps are the published PAYG GPU rate minus USD 0.01. Your cap is checked at launch. Storage and other charges are separate."}


def create_policy(name, max_price):
    name, maximum = pricing.policy_name(name), pricing.amount(max_price)
    with core.mutation_guard(resource="global-pricing"):
        value = _load()
        found = next((row for row in value["policies"] if row["name"] == name), None)
        if found:
            if found["max_price"] != maximum:
                raise core.NebiusError("That policy name already has a different cap. Choose another name or edit it")
            return copy.deepcopy(found)
        row = {"id": "spotpolicy-" + secrets.token_hex(12), "name": name, "max_price": maximum,
               "currency": "USD", "resource_version": "1"}
        value["policies"].append(row)
        _save(value)
        return row


def update_policy(policy_id, name, max_price, expected_version):
    name, maximum = pricing.policy_name(name), pricing.amount(max_price)
    with core.mutation_guard(resource="global-pricing"):
        value = _load()
        row = _find(value, policy_id)
        if row["resource_version"] != str(expected_version):
            raise core.NebiusError("The policy changed since review. Refresh and review its new terms")
        if any(p["id"] != policy_id and p["name"] == name for p in value["policies"]):
            raise core.NebiusError("That policy name already exists. Choose another name")
        if row["name"] != name or row["max_price"] != maximum:
            row.update(name=name, max_price=maximum, resource_version=str(int(row["resource_version"]) + 1))
        for platform, default_id in value["default_policy_ids"].items():
            if policy_id == default_id:
                _check_maximum(row, platform)
        value["policies"] = [row if p["id"] == policy_id else p for p in value["policies"]]
        _save(value)
        return row


def _check_maximum(row, platform):
    if platform in core.ON_DEMAND_GPU_USD:
        maximum = pricing.default_max_price(platform)
        if Decimal(row["max_price"]) > Decimal(maximum):
            raise core.NebiusError(f"This cap exceeds the published maximum for {catalog.gpu_name(platform)}: USD {maximum}/GPU-hour. "
                                  "Edit the cap in Settings or select a lower cap")


def select_default(policy_id, platforms):
    if (not isinstance(platforms, list) or not platforms or
            any(not isinstance(platform, str) or not re.fullmatch(r"gpu-[a-z0-9-]+", platform) for platform in platforms)):
        raise core.NebiusError("Choose the GPU platforms that should use this default")
    with core.mutation_guard(resource="global-pricing"):
        value = _load()
        row = _find(value, policy_id)
        for platform in platforms:
            _check_maximum(row, platform)
            value["default_policy_ids"][platform] = policy_id
        _save(value)
        return row


def existing_policies():
    """Offer old caps for explicit import without choosing a regional default."""
    rows, warnings = {}, []
    for project in core.sync_personal_projects()["projects"]:
        try:
            for row in pricing.list_policies(project["project_id"])["policies"]:
                if row["currency"] == "USD" and ID_LABEL not in row["labels"]:
                    rows.setdefault((row["name"], row["max_price"]), row)
        except core.NebiusError:
            warnings.append("Some existing policies could not be loaded. Refresh to try again.")
    return {"policies": sorted(rows.values(), key=lambda row: (row["name"], row["max_price"])),
            "warnings": sorted(set(warnings))}


def import_policy(policy_id, name, expected_version):
    row = pricing.get_policy(policy_id)
    pricing._scope(row["project_id"])
    if row["resource_version"] != str(expected_version):
        raise core.NebiusError("The policy changed since review. Refresh before importing its cap")
    if row["currency"] != "USD":
        raise core.NebiusError("Global policies use USD. This policy cannot be imported")
    return create_policy(name, row["max_price"])


def _cloud_name(row, platform):
    return "omarchy-" + row["id"] + "-v" + row["resource_version"] + "-" + platform


def _labels(row):
    return {ID_LABEL: row["id"], VERSION_LABEL: row["resource_version"]}


def _verify_copy(row, saved):
    if not pricing.matches_policy_identity(row, _cloud_name(saved, row["platform"]), _labels(saved)):
        raise core.NebiusError("The cloud policy's identifying labels conflict with your saved cap. "
                              "Review it in Nebius console or select another policy")
    if row["max_price"] != saved["max_price"] or row["currency"] != saved["currency"]:
        raise core.NebiusError("The cloud policy differs from your saved cap. Review it in Nebius console or select another policy")


def resolve(project_id, platform, mode="default", policy_id=""):
    if mode not in {"default", "policy", "follow"} or (mode != "policy" and policy_id):
        raise core.NebiusError("Choose default, policy, or follow spot pricing; only policy mode takes a policy ID")
    # Explicit cloud IDs remain usable for existing VMs and older integrations.
    if mode == "follow" or (mode == "policy" and policy_id.startswith("pricingpolicy-")):
        return pricing.resolve(project_id, platform, mode, policy_id)
    value = _load()
    if mode == "default":
        policy_id = value["default_policy_ids"].get(platform)
        if not policy_id:
            raise core.NebiusError("No default is available for this GPU. Select a saved cap or Follow spot price")
    saved = _find(value, policy_id)
    _check_maximum(saved, platform)
    result = pricing.list_policies(project_id)
    matches = [row for row in result["policies"] if row["name"] == _cloud_name(saved, platform) or
               (row["platform"] == platform and all(row["labels"].get(k) == v for k, v in _labels(saved).items()))]
    if len(matches) > 1:
        raise core.NebiusError("Several cloud policies match this cap. Resolve the duplicates in Nebius console before launching")
    row = matches[0] if matches else None
    if row:
        # A previous create may have completed without returning its labels.
        # Read the full resource and verify its scope, identity and exact cap.
        row = pricing.get_policy(row["id"], project_id, platform)
        _verify_copy(row, saved)
    else:
        row = {"id": "", "name": saved["name"], "project_id": project_id, "platform": platform,
               "max_price": saved["max_price"], "currency": saved["currency"], "resource_version": "",
               "state": "PLANNED", "scheduling_state": "PENDING_CREATION", "running_vm_count": 0}
    return {"mode": "policy", "policy": {**row, "display_name": saved["name"]},
            "global_policy": saved, "create_default": not bool(row["id"])}


def refresh_terms(selection, project_id, platform, *, create=False, require_allowed=True):
    with core.mutation_guard(resource="global-pricing"):
        saved = selection["global_policy"]
        if _find(_load(), saved["id"]) != saved:
            raise core.NebiusError("The global pricing policy changed since review. Review a fresh plan before continuing")
        _check_maximum(saved, platform)
        previous = selection["policy"]
        if previous["project_id"] != project_id or previous["platform"] != platform:
            raise core.NebiusError("The pricing placement changed since review. Review a fresh plan")
        if selection.get("create_default"):
            if not create:
                return selection
            row = pricing.create_policy(project_id, platform, _cloud_name(saved, platform), saved["max_price"],
                                        labels=_labels(saved))
        else:
            row = pricing.get_policy(previous["id"], project_id, platform)
            if pricing.terms({"mode": "policy", "policy": row}) != pricing.terms(selection):
                raise core.NebiusError("The pricing policy changed since review. Review a fresh plan before continuing")
        _verify_copy(row, saved)
        result = {"mode": "policy", "policy": {**row, "display_name": saved["name"]},
                  "global_policy": saved, "create_default": False}
        state = pricing.check(result)
        if require_allowed and state["state"] != "ok":
            raise core.NebiusError(state["message"] + ". No new disk or VM was allocated. Review Spot pricing")
        return result


def display_name(row):
    policy_id = row.get("labels", {}).get(ID_LABEL)
    if policy_id:
        try:
            return _find(_load(), policy_id)["name"]
        except core.NebiusError:
            pass
    return row["name"]
