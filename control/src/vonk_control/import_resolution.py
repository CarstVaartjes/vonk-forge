"""Resolve external identities and typed overlays for one WorkloadRun import."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass

from .import_report import ImportDisposition, ImportReportItem
from .model_resolution import ModelTransport, resolve_huggingface_snapshot
from .recipe_contract import RecipeContractError, validate_recipe
from .registry_resolution import RegistryTransport, resolve_public_image
from .source_bundles import GeneratedSourceBundle, generate_source_bundle
from .source_policy import SourcePolicyError, enforce_build_source_policy
from .workload_run_importer import WorkloadRunImportResult


class ImportResolutionError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    document: dict[str, object]
    bundle: GeneratedSourceBundle
    report: tuple[ImportReportItem, ...]
    blockers: tuple[ImportReportItem, ...]
    runnable: bool


def resolve_import(
    imported: WorkloadRunImportResult,
    overlays: Mapping[str, object],
    *,
    registry: RegistryTransport,
    models: ModelTransport,
) -> ResolutionResult:
    document = copy.deepcopy(imported.draft_document)
    dockerfile = imported.bundle.files.get("Dockerfile")
    if dockerfile is None:
        raise ImportResolutionError(
            "import.dockerfile_missing", "generated Dockerfile is missing"
        )
    try:
        dockerfile_text = dockerfile.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ImportResolutionError(
            "import.dockerfile_invalid", "generated Dockerfile is invalid"
        ) from error
    first, separator, remainder = dockerfile_text.partition("\n")
    if not first.startswith("FROM "):
        raise ImportResolutionError(
            "import.dockerfile_invalid", "generated Dockerfile has no base"
        )
    image = resolve_public_image(first.removeprefix("FROM "), registry)
    files = dict(imported.bundle.files)
    files["Dockerfile"] = f"FROM {image.reference}{separator}{remainder}".encode()
    bundle = generate_source_bundle(files)
    build = _mapping(document["build"])
    context = _mapping(build["context"])
    context["sha256"] = bundle.sha256
    context["expected_bytes"] = len(bundle.archive)
    artifacts = document["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise ImportResolutionError(
            "import.artifact_shape", "import artifact shape is invalid"
        )
    artifact = _mapping(artifacts[0])
    snapshot = resolve_huggingface_snapshot(
        str(artifact["repository"]), str(artifact["revision"]), models
    )
    artifact_sizes = overlays.get("artifact_sizes")
    weights = (
        artifact_sizes.get("weights") if isinstance(artifact_sizes, Mapping) else None
    )
    if not isinstance(weights, Mapping):
        raise ImportResolutionError(
            "import.artifact_sizes_required", "artifact size overlay is required"
        )
    artifact["revision"] = snapshot.revision
    artifact["download_bytes"] = _positive_int(weights, "download_bytes", "artifact")
    artifact["installed_bytes"] = _positive_int(weights, "installed_bytes", "artifact")
    if int(artifact["download_bytes"]) < snapshot.expected_bytes:
        raise ImportResolutionError(
            "import.artifact_sizes_invalid",
            "artifact download size is smaller than provider metadata",
        )
    build_resources = overlays.get("build_resources")
    if not isinstance(build_resources, Mapping):
        raise ImportResolutionError(
            "import.build_resources_required", "build resource overlay is required"
        )
    build["resources"] = {
        "download_bytes": _nonnegative_int(build_resources, "download_bytes", "build"),
        "temporary_bytes": _positive_int(build_resources, "temporary_bytes", "build"),
        "memory_bytes": _positive_int(build_resources, "memory_bytes", "build"),
        "timeout_seconds": _positive_int(build_resources, "timeout_seconds", "build"),
    }
    if overlays.get("security_acknowledged") is not True:
        raise ImportResolutionError(
            "import.security_required", "security overlay acknowledgement is required"
        )
    catalog_references = overlays.get("catalog_references")
    if not isinstance(catalog_references, Mapping):
        raise ImportResolutionError(
            "import.catalog_references_required",
            "exact catalog reference overlay is required",
        )
    model_reference = catalog_references.get("model")
    harness_reference = catalog_references.get("execution_harness")
    distribution_reference = catalog_references.get("runtime_distribution")
    patch_reference = catalog_references.get("patch_bundle")
    if (
        not isinstance(model_reference, Mapping)
        or not isinstance(harness_reference, Mapping)
        or not isinstance(distribution_reference, Mapping)
        or (patch_reference is not None and not isinstance(patch_reference, Mapping))
    ):
        raise ImportResolutionError(
            "import.catalog_references_required",
            "model, harness, distribution, and nullable patch references are required",
        )
    document["model"] = copy.deepcopy(dict(model_reference))
    execution = _mapping(document["execution"])
    execution["harness"] = copy.deepcopy(dict(harness_reference))
    execution["patch_bundle"] = (
        None if patch_reference is None else copy.deepcopy(dict(patch_reference))
    )
    runtime = _mapping(document["runtime"])
    runtime["distribution"] = copy.deepcopy(dict(distribution_reference))
    topology_resources = overlays.get("topology_resources")
    if not isinstance(topology_resources, Mapping):
        raise ImportResolutionError(
            "import.topology_resources_required",
            "topology resource overlay is required",
        )
    topology = document.get("topology")
    if not isinstance(topology, dict):
        raise ImportResolutionError("import.topology_invalid", "topology is invalid")
    roles = topology.get("roles")
    if not isinstance(roles, list):
        raise ImportResolutionError(
            "import.topology_invalid", "topology roles are invalid"
        )
    for role_value in roles:
        role = _mapping(role_value)
        supplied_role = topology_resources.get(str(role["name"]))
        if not isinstance(supplied_role, Mapping):
            raise ImportResolutionError(
                "import.topology_resources_required",
                f"resources for role {role['name']} are required",
            )
        disk = supplied_role.get("disk")
        memory = supplied_role.get("memory")
        if not isinstance(disk, Mapping) or not isinstance(memory, Mapping):
            raise ImportResolutionError(
                "import.topology_resources_invalid",
                "role disk and memory resources are required",
            )
        normalized_disk = {
            key: _nonnegative_int(disk, key, "disk")
            for key in (
                "image_bytes",
                "artifact_bytes",
                "staging_bytes",
                "cache_bytes",
                "rollback_bytes",
                "safety_margin_bytes",
            )
        }
        if normalized_disk["artifact_bytes"] < int(artifact["installed_bytes"]):
            raise ImportResolutionError(
                "import.topology_resources_invalid",
                "role artifact bytes are smaller than installed artifacts",
            )
        kind = memory.get("kind")
        if kind not in {"unified", "host", "accelerator"}:
            raise ImportResolutionError(
                "import.topology_resources_invalid", "role memory kind is invalid"
            )
        normalized_memory: dict[str, object] = {
            "kind": kind,
            "startup_peak_bytes": _positive_int(memory, "startup_peak_bytes", "memory"),
            "steady_state_bytes": _positive_int(memory, "steady_state_bytes", "memory"),
            "runtime_growth_bytes": _nonnegative_int(
                memory, "runtime_growth_bytes", "memory"
            ),
            "system_reserve_bytes": _nonnegative_int(
                memory, "system_reserve_bytes", "memory"
            ),
        }
        role["resources"] = {"disk": normalized_disk, "memory": normalized_memory}
    if int(topology["node_count"]) > 1:
        fabric = overlays.get("topology_fabric")
        if not isinstance(fabric, Mapping):
            raise ImportResolutionError(
                "import.topology_required", "topology fabric is required"
            )
        topology["fabric"] = copy.deepcopy(dict(fabric))
    resolved_items: list[ImportReportItem] = []
    for item in imported.report:
        handled = (
            item.source_path
            in {
                "/container",
                "/model_revision",
                "/runtime",
                "/@missing/resources",
                "/@missing/security",
                "/@missing/topology-fabric",
            }
            or item.reason_code == "runtime.environment_review"
        )
        if handled and item.disposition in {
            ImportDisposition.RESOLUTION_REQUIRED,
            ImportDisposition.OVERLAY_REQUIRED,
        }:
            resolved_items.append(
                ImportReportItem(
                    item.source_path,
                    ImportDisposition.RESOLVED,
                    item.destination_path,
                    f"{item.reason_code}.resolved",
                    f"Resolved: {item.detail}",
                    False,
                )
            )
        else:
            resolved_items.append(item)
    blockers = tuple(
        item
        for item in resolved_items
        if item.blocking
        or item.disposition
        in {
            ImportDisposition.RESOLUTION_REQUIRED,
            ImportDisposition.OVERLAY_REQUIRED,
            ImportDisposition.UNSUPPORTED_BLOCKING,
        }
    )
    if not blockers:
        try:
            validate_recipe(document)
        except RecipeContractError as error:
            raise ImportResolutionError(
                error.code, f"{error.path}: {error.detail}"
            ) from error
        try:
            enforce_build_source_policy(document, bundle)
        except SourcePolicyError as error:
            finding = error.report.findings[0]
            raise ImportResolutionError(finding.code, finding.detail) from error
    return ResolutionResult(
        document, bundle, tuple(resolved_items), blockers, not blockers
    )


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ImportResolutionError(
            "import.document_invalid", "import document is invalid"
        )
    return value


def _positive_int(values: Mapping[str, object], key: str, scope: str) -> int:
    value = values.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ImportResolutionError(
            f"import.{scope}_resources_invalid", f"{scope} resource {key} is invalid"
        )
    return value


def _nonnegative_int(values: Mapping[str, object], key: str, scope: str) -> int:
    value = values.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ImportResolutionError(
            f"import.{scope}_resources_invalid", f"{scope} resource {key} is invalid"
        )
    return value
