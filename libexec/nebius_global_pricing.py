"""Installation-wide Spot caps, materialized in the launch project's platform.

Saved revisions are immutable in the cloud. Editing a cap changes future
selections, while existing VMs keep the terms that were reviewed for them.
"""

import copy
import re
import secrets

import nebius_core as core
import nebius_pricing as pricing


SCHEMA = "nebius.omarchy-global-pricing/v1"
DEFAULT_ID = "spotpolicy-default"
ID_LABEL = "pricing-global-id"
VERSION_LABEL = "pricing-global-version"


def _initial():
    return {"schema": SCHEMA, "default_policy_id": "", "policies": [{
        "id": DEFAULT_ID, "name": "Default cap", "max_price": pricing.DEFAULT_MAX_PRICE,
        "currency": "USD", "resource_version": "1"}]}


def _load():
    path = core.STATE_DIR / "global-pricing.json"
    value = core._read_json(path, None)
    if value is None and not path.exists():
        return _initial()
    try:
        if value["schema"] != SCHEMA:
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
        if not isinstance(value["default_policy_id"], str) or (value["default_policy_id"] or DEFAULT_ID) not in ids:
            raise ValueError("Missing default")
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
    return {"policies": sorted(value["policies"], key=lambda row: (row["name"], row["id"])),
            "default_policy_id": value["default_policy_id"] or DEFAULT_ID,
            "default_max_price": pricing.DEFAULT_MAX_PRICE, "default_currency": "USD",
            "range_note": "Your cap is checked at launch. Storage and other charges are separate."}


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
        value["policies"] = [row if p["id"] == policy_id else p for p in value["policies"]]
        _save(value)
        return row


def select_default(policy_id):
    with core.mutation_guard(resource="global-pricing"):
        value = _load()
        row = _find(value, policy_id)
        value["default_policy_id"] = policy_id
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
    if (row["max_price"] != saved["max_price"] or row["currency"] != saved["currency"] or
            row["labels"].get("managed-by") != core.MANAGED_BY or
            any(row["labels"].get(key) != value for key, value in _labels(saved).items())):
        raise core.NebiusError("The cloud policy differs from your saved cap. Review it in Nebius console or select another policy")


def resolve(project_id, platform, mode="default", policy_id=""):
    if mode not in {"default", "policy", "follow"} or (mode != "policy" and policy_id):
        raise core.NebiusError("Choose default, policy, or follow spot pricing; only policy mode takes a policy ID")
    # Explicit cloud IDs remain usable for existing VMs and older integrations.
    if mode == "follow" or (mode == "policy" and policy_id.startswith("pricingpolicy-")):
        return pricing.resolve(project_id, platform, mode, policy_id)
    value = _load()
    saved = _find(value, policy_id if mode == "policy" else value["default_policy_id"] or DEFAULT_ID)
    result = pricing.list_policies(project_id, platform)
    if mode == "default" and not value["default_policy_id"]:
        legacy_default = result["default_policy_id"] or any(
            row["labels"].get("managed-by") == core.MANAGED_BY and row["labels"].get("pricing-default") == "true"
            for row in result["policies"])
        if legacy_default:
            raise core.NebiusError("An older default exists. Import its cap or choose a global default in Settings → Spot pricing policies before launching")
    matches = [row for row in result["policies"] if all(row["labels"].get(k) == v for k, v in _labels(saved).items())]
    if len(matches) > 1:
        raise core.NebiusError("Several cloud policies match this cap. Resolve the duplicates in Nebius console before launching")
    row = matches[0] if matches else None
    if row:
        _verify_copy(row, saved)
    else:
        if any(p["name"] == _cloud_name(saved, platform) for p in result["policies"]):
            raise core.NebiusError("The cloud policy name is already in use. Select another global policy")
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
