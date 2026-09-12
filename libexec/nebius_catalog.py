"""Read-only image discovery and user-facing GPU configuration grouping."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import re

import nebius_core as core


# Only known equivalent GPU products share a label. Unknown platforms keep their name.
GPU_NAMES = {
    "gpu-rtx6000": "RTX PRO 6000", "gpu-rtx6000-a": "RTX PRO 6000",
    "gpu-l40s-a": "L40S", "gpu-l40s-d": "L40S",
    "gpu-h100-sxm": "H100", "gpu-h200-sxm": "H200",
    "gpu-b200-sxm": "B200", "gpu-b200-sxm-a": "B200", "gpu-b300-sxm": "B300",
}
# These documented platforms have Intel/AMD x86 CPUs; GPU architecture is separate.
CPU_ARCHITECTURES = dict.fromkeys(GPU_NAMES, "amd64")


def gpu_name(platform, fallback="GPU"):
    return GPU_NAMES.get(platform, platform.removeprefix("gpu-").replace("-", " ").upper() if platform else fallback)


def configuration_key(row):
    return (row.get("tenant_id"), row["region"], gpu_name(row.get("platform", ""), row.get("gpu_label", "GPU")),
            row.get("gpu_count"), row.get("vcpu_count"), row.get("memory_gib"), row.get("gpu_memory_gb"),
            CPU_ARCHITECTURES.get(row.get("platform")))


def configurations(offerings, allocation):
    groups = {}
    for row in offerings:
        groups.setdefault(configuration_key(row), []).append(row)
    result = []
    for variants in groups.values():
        best = max(variants, key=lambda row: core._availability_score(row[allocation]))
        projects = {p["project_id"]: p for row in variants for p in row.get("projects", [])}
        grouped = {**best, "gpu_label": gpu_name(best.get("platform", ""), best.get("gpu_label", "GPU")),
                   "variants": variants, "projects": sorted(projects.values(), key=lambda p: p["project_name"])}
        for mode in ("on_demand", "preemptible"):
            # Advice may overlap across fabrics: report the best pool, never sum it.
            grouped[mode] = max((row[mode] for row in variants), key=core._availability_score)
        result.append(grouped)
    return sorted(result, key=lambda row: (int(row.get("gpu_count") or 0), row["region"],
                                          int(row.get("vcpu_count") or 0), int(row.get("memory_gib") or 0)))


def resolve_variant(offering, project_id, allocation, compatible_ids=None):
    candidates = [row for row in offering.get("variants", [offering])
                  if project_id in {p["project_id"] for p in row.get("projects", [])}
                  and (allocation != "preemptible" or not any(
                      p["project_id"] == project_id and p.get("allowed_for_preemptibles") is False
                      for p in row.get("projects", [])))
                  and core._availability_score(row[allocation])[0] >= 0
                  and (compatible_ids is None or row["offering_id"] in compatible_ids)]
    if not candidates:
        raise core.NebiusError("No available configuration matches this project and image. Change image, allocation or project.")
    def rank(row):
        price = core._hourly_estimate(row["platform"], row["gpu_count"], row["vcpu_count"], row["memory_gib"], allocation)
        score = core._availability_score(row[allocation])
        return (-score[0], -score[1], price if price is not None else math.inf, row["offering_id"])
    return sorted(candidates, key=rank)[0]


def compatibility(image, offering):
    spec, status = image.get("spec", {}), image.get("status", {})
    reasons, warnings = [], []
    if status.get("state") != "READY" or status.get("reconciling"):
        reasons.append("Image is not ready")
    architecture = str(spec.get("cpu_architecture") or "unspecified").lower()
    target = offering.get("cpu_architecture") or CPU_ARCHITECTURES.get(offering["platform"])
    if architecture in {"amd64", "arm64"} and target and architecture != target:
        reasons.append(f"Image requires {architecture}; this machine uses {target}")
    elif architecture not in {"amd64", "arm64"} or not target:
        warnings.append("CPU architecture compatibility is not specified")
    unsupported = spec.get("unsupported_platforms") or {}
    if offering["platform"] in unsupported:
        reasons.append(unsupported[offering["platform"]] or "Platform is unsupported")
    for preset in spec.get("unsupported_presets") or []:
        if preset.get("platform") == offering["platform"] and preset.get("preset") == offering.get("preset"):
            reasons.append(preset.get("reason") or "Machine size is unsupported")
    if offering["platform"] not in (spec.get("recommended_platforms") or []):
        warnings.append("Image does not declare this GPU platform as recommended; verify GPU drivers and cloud-init")
    return reasons, warnings


def image_row(image, offerings, source):
    compatible, warnings = [], set()
    for offering in offerings:
        reasons, notes = compatibility(image, offering)
        if not reasons:
            compatible.append(offering["offering_id"])
            warnings.update(notes)
    if not compatible:
        return None
    meta, spec, status = image.get("metadata", {}), image.get("spec", {}), image.get("status", {})
    labels = meta.get("labels") or {}
    summary = " · ".join(str(labels[key]) for key in ("os_name", "os_version") if labels.get(key))
    if labels.get("cuda_toolkit"):
        summary += " · CUDA " + str(labels["cuda_toolkit"])
    return {"image_id": meta["id"], "name": meta.get("name") or meta["id"], "source": source,
            "image_family": spec.get("image_family", ""), "description": spec.get("description", ""), "summary": summary.strip(" ·"),
            "cpu_architecture": str(spec.get("cpu_architecture") or "unspecified").lower(),
            "min_disk_gib": math.ceil(int(status.get("min_disk_size_bytes") or 0) / 1024**3),
            "compatible_offering_ids": compatible, "warnings": sorted(warnings)}


def list_images(offering_ids, project_id):
    capacity = core.gpu_capacity()
    offerings = [row for row in capacity["offerings"] if row["offering_id"] in offering_ids
                 and project_id in {p["project_id"] for p in row.get("projects", [])}]
    if not offerings or len({(row.get("tenant_id"), row["region"]) for row in offerings}) != 1:
        raise core.NebiusError("Choose a configuration and compatible project before selecting an image")
    region = offerings[0]["region"]
    tenant = core.profile_value("tenant-id")
    images, warnings = {}, []
    sources = []
    try:
        projects = core._items(core.run_cli(["iam", "project", "list", "--parent-id", tenant,
                                            "--all", "--format", "json"], timeout=25))
        for item in projects:
            try:
                project = core._project_record(item, tenant, "")
                if project["region"] == region:
                    sources.append((project["project_name"], ["compute", "image", "list", "--parent-id",
                                                              project["project_id"], "--all", "--format", "json"]))
            except core.NebiusError:
                warnings.append("Could not inspect one project's region; discovery is incomplete")
    except core.NebiusError:
        warnings.append("Could not list tenant projects; showing public images and the selected project")
        sources.append(("Selected project", ["compute", "image", "list", "--parent-id", project_id,
                                              "--all", "--format", "json"]))
    sources.append(("Public images", ["compute", "image", "list-public", "--region", region, "--format", "json"]))
    with ThreadPoolExecutor(max_workers=6) as executor:
        pending = {executor.submit(core.run_cli, command, timeout=25): label for label, command in sources}
        for future in as_completed(pending):
            source = pending[future]
            try:
                for image in core._items(future.result()):
                    row = image_row(image, offerings, source)
                    if row:
                        images[row["image_id"]] = row
            except core.NebiusError:
                warnings.append(f"Could not list images in {source}; discovery is incomplete")
    return {"region": region, "images": sorted(images.values(), key=lambda row: (row["source"], row["name"], row["image_id"])),
            "warnings": sorted(warnings)}


def get_image(image_id, offering):
    if not re.fullmatch(r"computeimage-[a-z0-9-]+", image_id):
        raise core.NebiusError("Invalid image ID")
    image = core.run_cli(["compute", "image", "get", "--id", image_id, "--format", "json"], timeout=25)
    reasons, _ = compatibility(image, offering)
    if reasons:
        raise core.NebiusError("Image cannot be used: " + "; ".join(reasons))
    parent = image.get("metadata", {}).get("parent_id", "")
    # Verify immutable regional placement, including public catalog images.
    if parent.endswith("public-images"):
        public = core._items(core.run_cli(["compute", "image", "list-public", "--region", offering["region"],
                                          "--format", "json"], timeout=25))
        if image_id not in {row.get("metadata", {}).get("id") for row in public}:
            raise core.NebiusError("The public image is not available in this region")
        return image
    project = core.run_cli(["iam", "project", "get", "--id", parent, "--format", "json"], timeout=25)
    region = project.get("spec", {}).get("region") or project.get("status", {}).get("region")
    if region != offering["region"]:
        raise core.NebiusError("The image must be in the same region as the VM")
    return image
