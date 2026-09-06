"""Durable source-build planning and exact OCI result recording."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .inventory_repository import InventoryRepository
from .models import (
    AgentNode,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    RecipeBuild,
    RecipeSourceBundle,
    ResourceReservation,
)
from .recipe_contract import RecipeContractError, recipe_topology
from .source_bundles import SourceBundleError, SourceBundleStore
from .source_policy import (
    SourcePolicyError,
    SourcePolicyReport,
    dockerfile_base_images,
    enforce_build_source_policy,
    inspect_build_source_policy,
)

_OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
BUILD_ARTIFACT_FORMAT = "docker-archive-v1"
MINIMUM_BUILD_DISK_RESERVE_BYTES = 4 * 1024**3
MAXIMUM_BUILD_DISK_RESERVE_BYTES = 64 * 1024**3
BUILD_INPUT_IDENTITY_SCHEMA_VERSION = 2


def derive_build_input_identity(
    build: Mapping[str, object],
    *,
    source_bundle_sha256: str,
    builder_binary_digest: str | None,
    artifact_format: str = BUILD_ARTIFACT_FORMAT,
    base_images: Sequence[Mapping[str, object]] = (),
    effective_settings: object | None = None,
    topology_inputs: Mapping[str, object] | None = None,
    model_artifacts: Sequence[object] | None = None,
) -> dict[str, object]:
    """Project the exact executable inputs used to build a recipe image.

    Catalog revision/content digests authorize and describe a request.  They
    are deliberately absent from this identity: changing notes, capability
    evidence, or provenance must not rebuild an unchanged executable.  Model
    selectors likewise contribute their canonical file content only when the
    caller says the build consumes those files; roles and mounts belong to the
    runtime execution identity.
    """
    executable_fields = {
        field: copy.deepcopy(build[field])
        for field in (
            "base_image",
            "context",
            "dockerfile",
            "target",
            "platform",
            "arguments",
            "network",
            "options",
            "security",
        )
        if field in build
    }
    identity: dict[str, object] = {
        "schema_version": BUILD_INPUT_IDENTITY_SCHEMA_VERSION,
        "source_bundle_sha256": source_bundle_sha256,
        "artifact_format": artifact_format,
        "base_images": copy.deepcopy(list(base_images)),
        "execution_build": executable_fields,
    }
    # A resolution intent deliberately omits this field.  It must never use
    # a fabricated digest: only a live builder or a recorded successful
    # receipt can supply the executable builder identity.
    if builder_binary_digest is not None:
        identity["builder_binary_digest"] = builder_binary_digest
    settings = _build_effective_settings(effective_settings)
    if settings:
        identity["effective_build_settings"] = settings
    if topology_inputs is not None:
        identity["topology_inputs"] = copy.deepcopy(dict(topology_inputs))
    if model_artifacts is not None:
        identity["model_artifacts"] = _canonical_model_build_inputs(model_artifacts)
    return identity


def _build_effective_settings(value: object | None) -> dict[str, object] | None:
    """Project only canonical settings with a ``rebuild`` change effect."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        # The public v2 contract stores every setting as {value,
        # change_effect}; older resolver objects expose a separate map.  Both
        # shapes are projections of the same canonical settings authority.
        effects = value.get("change_effects")
        if isinstance(effects, Mapping):
            return _select_rebuild_settings(value, effects)
        selected: dict[str, object] = {}
        selected_effects: dict[str, str] = {}
        for key, setting in value.items():
            if key in {"kind", "knobs"}:
                continue
            if isinstance(setting, Mapping) and setting.get("change_effect") == "rebuild":
                if "value" not in setting:
                    raise ValueError(f"rebuild setting {key!r} has no effective value")
                selected[key] = copy.deepcopy(setting["value"])
                selected_effects[key] = "rebuild"
        knobs = value.get("knobs")
        if isinstance(knobs, Mapping):
            for key, setting in knobs.items():
                if isinstance(setting, Mapping) and setting.get("change_effect") == "rebuild":
                    if "value" not in setting:
                        raise ValueError(f"rebuild setting {key!r} has no effective value")
                    selected[f"knobs.{key}"] = copy.deepcopy(setting["value"])
                    selected_effects[f"knobs.{key}"] = "rebuild"
        return (
            None
            if not selected_effects
            else {"values": selected, "change_effects": selected_effects}
        )

    effects = getattr(value, "change_effects", None)
    if not isinstance(effects, Sequence) or isinstance(effects, (str, bytes)):
        return None
    effect_map = {
        key: _change_effect_value(effect)
        for key, effect in effects
        if isinstance(key, str)
    }
    values: dict[str, object] = {}
    knobs = getattr(value, "knobs", None)
    knob_values = (
        {key: copy.deepcopy(item) for key, item in knobs if isinstance(key, str)}
        if isinstance(knobs, Sequence) and not isinstance(knobs, (str, bytes))
        else {}
    )
    for key, effect in effect_map.items():
        if effect != "rebuild":
            continue
        if key in knob_values:
            values[key] = knob_values[key]
        elif hasattr(value, key):
            values[key] = copy.deepcopy(getattr(value, key))
    selected_effects = {key: "rebuild" for key, effect in effect_map.items() if effect == "rebuild"}
    if len(values) != len(selected_effects):
        raise ValueError("rebuild settings must expose every effective value")
    return None if not selected_effects else {"values": values, "change_effects": selected_effects}


def _change_effect_value(value: object) -> object:
    return getattr(value, "value", value)


def _select_rebuild_settings(
    settings: Mapping[str, object], effects: Mapping[str, object]
) -> dict[str, object] | None:
    selected: dict[str, object] = {}
    for key, effect in effects.items():
        if _change_effect_value(effect) != "rebuild":
            continue
        found, setting = _lookup_setting(settings, key)
        if not found:
            raise ValueError(f"rebuild setting {key!r} has no effective value")
        selected[key] = copy.deepcopy(setting)
    selected_effects = {
        key: "rebuild" for key, effect in effects.items() if _change_effect_value(effect) == "rebuild"
    }
    return None if not selected_effects else {"values": selected, "change_effects": selected_effects}


def _lookup_setting(settings: Mapping[str, object], key: str) -> tuple[bool, object]:
    if key in settings:
        value = settings[key]
        if isinstance(value, Mapping) and "value" in value:
            return True, value["value"]
        return True, value
    knobs = settings.get("knobs")
    if isinstance(knobs, Mapping) and key in knobs:
        value = knobs[key]
        if isinstance(value, Mapping) and "value" in value:
            return True, value["value"]
        return True, value
    current: object = settings
    for component in key.split("."):
        if not isinstance(current, Mapping) or component not in current:
            return False, None
        current = current[component]
    if isinstance(current, Mapping) and "value" in current:
        return True, current["value"]
    return True, current


def _canonical_model_build_inputs(artifacts: Sequence[object]) -> list[dict[str, object]]:
    projected: list[dict[str, object]] = []
    for artifact in artifacts:
        if isinstance(artifact, Mapping):
            path = artifact.get("path")
            digest = artifact.get("sha256")
            size = artifact.get("download_bytes", artifact.get("size_bytes", artifact.get("size")))
        else:
            path = getattr(artifact, "path", None)
            digest = getattr(artifact, "sha256", None)
            size = getattr(artifact, "download_bytes", getattr(artifact, "size_bytes", getattr(artifact, "size", None)))
        if not isinstance(path, str) or not isinstance(digest, str) or not isinstance(size, int) or isinstance(size, bool):
            raise TypeError("model build inputs require canonical path, sha256, and size")
        projected.append({"path": path, "sha256": digest, "download_bytes": size})
    return sorted(projected, key=lambda item: (str(item["path"]), str(item["sha256"]), item["download_bytes"]))


def _canonical_build(document: Mapping[str, object]) -> Mapping[str, object]:
    execution = document.get("execution")
    if not isinstance(execution, Mapping) or execution.get("mode") != "build":
        raise RecipeBuildError("build.not_required", "recipe selects a prebuilt image")
    build = execution.get("build")
    if not isinstance(build, Mapping):
        raise RecipeBuildError("build.contract_invalid", "canonical execution.build is unavailable")
    return {**build, "dockerfile": _bundle_dockerfile_path(build)}


def _bundle_dockerfile_path(build: Mapping[str, object]) -> object:
    """Project the repository path into the verified build-context archive."""

    context = build.get("context")
    path = context.get("path") if isinstance(context, Mapping) else None
    dockerfile = build.get("dockerfile")
    if isinstance(path, str) and isinstance(dockerfile, str):
        return dockerfile.removeprefix(path.rstrip("/") + "/")
    return dockerfile


def _source_bundle_handle(revision: CatalogDocumentRevision) -> str:
    """Return the catalog-owned source package digest for a build.

    The package handle is a catalog projection.  A recipe document cannot
    smuggle source bytes or an arbitrary URL into the builder.
    """
    projected = revision.projected if isinstance(revision.projected, Mapping) else {}
    candidate = projected.get("package_handle")
    if candidate is None:
        candidate = projected.get("source_bundle")
    if isinstance(candidate, Mapping):
        candidate = candidate.get("sha256")
    if candidate is None:
        candidate = projected.get("source_bundle_sha256")
    if not isinstance(candidate, str) or _SHA256.fullmatch(candidate) is None:
        raise RecipeBuildError("build.source_unavailable", "catalog package handle is unavailable")
    return candidate


def _canonical_build_resources(
    revision: CatalogDocumentRevision,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Read compiler-owned build admission projections from the catalog row."""
    projected = revision.projected if isinstance(revision.projected, Mapping) else {}
    resources = projected.get("build_resources")
    security = projected.get("build_security")
    if not isinstance(resources, Mapping):
        raise RecipeBuildError(
            "build.resources_invalid",
            "canonical runtime compiler did not publish a build resource envelope",
        )
    required = (
        "temporary_bytes",
        "memory_bytes",
        "cpu_cores",
        "processes",
        "download_bytes",
        "timeout_seconds",
    )
    if any(
        not isinstance(resources.get(key), int)
        or isinstance(resources.get(key), bool)
        or int(resources[key]) < 0
        for key in required
    ):
        raise RecipeBuildError("build.resources_invalid", "canonical build resource envelope is invalid")
    if not isinstance(security, Mapping) or not isinstance(security.get("capabilities"), list):
        raise RecipeBuildError(
            "build.security_invalid",
            "canonical runtime compiler did not publish a build security envelope",
        )
    if any(not isinstance(item, str) or not item for item in security["capabilities"]):
        raise RecipeBuildError("build.security_invalid", "canonical build capabilities are invalid")
    return resources, security


def _canonical_build_platform(build: Mapping[str, object]) -> str:
    base_image = build.get("base_image")
    if not isinstance(base_image, Mapping) or base_image.get("platform") != "linux/arm64":
        raise RecipeBuildError(
            "build.platform_invalid", "canonical source builds require a linux/arm64 base image"
        )
    return "linux/arm64"


def _source_policy_document(
    document: Mapping[str, object],
    build: Mapping[str, object],
    source_sha256: str,
) -> dict[str, object]:
    """Adapt canonical execution.build to the source-policy parser boundary."""
    context = build.get("context")
    context_path = context.get("path") if isinstance(context, Mapping) else None
    if not isinstance(context_path, str):
        raise RecipeBuildError("build.source_invalid", "canonical build context path is invalid")
    normalized_build = {
        "context": {"path": context_path, "sha256": source_sha256},
        "dockerfile": build.get("dockerfile"),
        "network": copy.deepcopy(build.get("network", {"mode": "none", "hosts": []})),
    }
    return {**copy.deepcopy(dict(document)), "build": normalized_build}


def _build_disk_envelope(
    *, base_image_bytes: int, temporary_bytes: int, source_bytes: int, output_bytes: int
) -> int:
    """Peak working-space admission envelope, excluding the host reserve."""
    return max(temporary_bytes, base_image_bytes + source_bytes + output_bytes)


def _build_disk_reserve(disk_total_bytes: int) -> int:
    """Leave two percent free, capped at 64 GiB on Spark-sized disks."""
    return min(
        disk_total_bytes // 4,
        max(
            MINIMUM_BUILD_DISK_RESERVE_BYTES,
            min(MAXIMUM_BUILD_DISK_RESERVE_BYTES, disk_total_bytes // 50),
        ),
    )


class RecipeBuildError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class RecipeBuildPlan:
    build_id: str
    recipe_revision_id: str
    recipe_content_sha256: str
    builder_node_id: str
    source_bundle_sha256: str
    build_input_sha256: str
    agent_payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class RecipeBuildResolution:
    """Builder-independent source-build resolution.

    ``input_intent_sha256`` is an immutable request identity, not an
    executable build identity.  ``build_input_sha256`` is populated only
    when a succeeded receipt was found and verified with its recorded
    builder binary digest.  The selected live builder must still be admitted
    and planned before a new final build identity is usable for dispatch.
    """

    recipe_revision_id: str
    recipe_content_sha256: str
    source_bundle_sha256: str
    input_intent_sha256: str
    input_intent: dict[str, object]
    build_input_sha256: str | None = None
    build_id: str | None = None
    builder_node_id: str | None = None
    builder_binary_digest: str | None = None
    image_digest: str | None = None
    oci_layout_sha256: str | None = None
    image_bytes: int | None = None

    @property
    def cached(self) -> bool:
        return self.build_id is not None


@dataclass(frozen=True, slots=True)
class CompletedRecipeBuild:
    build_id: str
    image_digest: str
    oci_layout_sha256: str
    image_bytes: int


@dataclass(frozen=True, slots=True)
class ImageDistributionPlan:
    build_id: str
    mapping_id: str
    mapping_generation: int
    image_digest: str
    targets: tuple[tuple[str, dict[str, object]], ...]


class RecipeBuildService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        bundles: SourceBundleStore,
        inventory_max_age: int = 300,
    ) -> None:
        self._sessions = sessions
        self._bundles = bundles
        self._inventory = InventoryRepository(sessions)
        self._inventory_max_age = inventory_max_age

    def check_source(self, recipe_revision_id: str) -> SourcePolicyReport:
        with self._sessions() as session:
            revision = session.get(CatalogDocumentRevision, recipe_revision_id)
            if revision is None:
                raise KeyError(recipe_revision_id)
            if revision.kind != "recipe" or revision.state != "active":
                raise RecipeBuildError(
                    "build.recipe_unresolved", "only a resolved recipe can be checked"
                )
            document = copy.deepcopy(revision.document)
            build = _canonical_build(document)
            source_sha256 = _source_bundle_handle(revision)
            if (
                session.get(RecipeSourceBundle, source_sha256) is None
            ):
                raise RecipeBuildError(
                    "build.source_unavailable", "verified source bundle is unavailable"
                )
        try:
            bundle = self._bundles.get(source_sha256)
        except SourceBundleError as error:
            raise RecipeBuildError(error.code, str(error)) from error
        return inspect_build_source_policy(
            _source_policy_document(document, build, source_sha256), bundle
        )

    def resolve(self, recipe_revision_id: str) -> RecipeBuildResolution:
        """Resolve immutable source-build inputs and an exact cached receipt.

        This method intentionally performs no builder lookup, inventory read,
        or capacity admission.  A cache hit is accepted only when the
        current canonical recipe/source policy and executable build inputs
        reproduce the succeeded row's exact final build identity.  A row's
        package handle, notes, or source digest alone is never sufficient.
        """
        with self._sessions() as session:
            revision = session.get(CatalogDocumentRevision, recipe_revision_id)
            if revision is None:
                raise KeyError(recipe_revision_id)
            if revision.kind != "recipe" or revision.state != "active":
                raise RecipeBuildError(
                    "build.recipe_unresolved", "only a resolved recipe can be built"
                )
            document = copy.deepcopy(revision.document)
            build = _canonical_build(document)
            source_sha256 = _source_bundle_handle(revision)
            if session.get(RecipeSourceBundle, source_sha256) is None:
                raise RecipeBuildError(
                    "build.source_unavailable", "verified source bundle is unavailable"
                )

        try:
            bundle = self._bundles.get(source_sha256)
            enforce_build_source_policy(
                _source_policy_document(document, build, source_sha256), bundle
            )
        except SourceBundleError as error:
            raise RecipeBuildError(error.code, str(error)) from error
        except SourcePolicyError as error:
            finding = error.report.findings[0]
            raise RecipeBuildError(finding.code, finding.detail) from error

        dockerfile_path = build.get("dockerfile")
        dockerfile_payload = (
            bundle.files.get(dockerfile_path)
            if isinstance(dockerfile_path, str)
            else None
        )
        if dockerfile_payload is None:
            raise RecipeBuildError(
                "build.source_invalid", "recipe Dockerfile authority is unavailable"
            )
        base_images = list(dockerfile_base_images(dockerfile_payload))
        _canonical_build_resources(revision)
        _canonical_build_platform(build)
        _declared_image_bytes(document)
        projected = revision.projected if isinstance(revision.projected, Mapping) else {}
        model_inputs = projected.get("build_model_artifacts")
        topology_inputs = projected.get("build_topology_inputs")
        model_artifacts = (
            model_inputs
            if isinstance(model_inputs, Sequence)
            and not isinstance(model_inputs, (str, bytes))
            else None
        )
        topology = topology_inputs if isinstance(topology_inputs, Mapping) else None
        intent = derive_build_input_identity(
            build,
            source_bundle_sha256=source_sha256,
            builder_binary_digest=None,
            artifact_format=BUILD_ARTIFACT_FORMAT,
            base_images=base_images,
            effective_settings=document.get("settings"),
            topology_inputs=topology,
            model_artifacts=model_artifacts,
        )
        intent_sha256 = _digest(intent)

        cached: RecipeBuild | None = None
        with self._sessions() as session:
            candidates = session.scalars(
                select(RecipeBuild)
                .where(
                    RecipeBuild.source_bundle_sha256 == source_sha256,
                    RecipeBuild.state == "succeeded",
                )
                .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
            )
            for candidate in candidates:
                if not _valid_succeeded_receipt(candidate):
                    continue
                report = candidate.policy_report
                builder_digest = (
                    report.get("builder_binary_digest")
                    if isinstance(report, Mapping)
                    else None
                )
                if (
                    not isinstance(builder_digest, str)
                    or _SHA256.fullmatch(builder_digest) is None
                    or not isinstance(report, Mapping)
                    or report.get("artifact_format") != BUILD_ARTIFACT_FORMAT
                    or report.get("source_bundle_sha256") != source_sha256
                ):
                    continue
                exact = derive_build_input_identity(
                    build,
                    source_bundle_sha256=source_sha256,
                    builder_binary_digest=builder_digest,
                    artifact_format=BUILD_ARTIFACT_FORMAT,
                    base_images=base_images,
                    effective_settings=document.get("settings"),
                    topology_inputs=topology,
                    model_artifacts=model_artifacts,
                )
                if candidate.build_input_sha256 != _digest(exact):
                    continue
                cached = candidate
                break

        if cached is None:
            return RecipeBuildResolution(
                recipe_revision_id=revision.id,
                recipe_content_sha256=revision.content_digest,
                source_bundle_sha256=source_sha256,
                input_intent_sha256=intent_sha256,
                input_intent=copy.deepcopy(intent),
            )
        report = cached.policy_report
        builder_digest = report["builder_binary_digest"]
        return RecipeBuildResolution(
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            source_bundle_sha256=source_sha256,
            input_intent_sha256=intent_sha256,
            input_intent=copy.deepcopy(intent),
            build_input_sha256=cached.build_input_sha256,
            build_id=cached.id,
            builder_node_id=cached.builder_node_id,
            builder_binary_digest=builder_digest,
            image_digest=cached.image_digest,
            oci_layout_sha256=cached.oci_layout_sha256,
            image_bytes=cached.image_bytes,
        )

    def plan(
        self,
        recipe_revision_id: str,
        builder_node_id: str,
        *,
        now: datetime,
        resolution: RecipeBuildResolution | None = None,
    ) -> RecipeBuildPlan:
        with self._sessions() as session:
            revision = session.get(CatalogDocumentRevision, recipe_revision_id)
            if revision is None:
                raise KeyError(recipe_revision_id)
            if revision.kind != "recipe" or revision.state != "active":
                raise RecipeBuildError(
                    "build.recipe_unresolved", "only a resolved recipe can be built"
                )
            node = session.get(AgentNode, builder_node_id)
            if node is None:
                raise RecipeBuildError(
                    "build.node_unknown", "builder GPU node is unknown"
                )
            _validate_builder(node)
            document = copy.deepcopy(revision.document)
            build = _canonical_build(document)
            source_sha256 = _source_bundle_handle(revision)
            public_network = _public_build_network(build)
            if (
                public_network
                and "recipe.build.egress-proxy.v1" not in node.capabilities
            ):
                raise RecipeBuildError(
                    "build.network_capability_missing",
                    "builder does not advertise the hostname-aware build egress boundary",
                )
            assert node.binary_digest is not None
            builder_binary_digest = node.binary_digest
            stored = session.get(RecipeSourceBundle, source_sha256)
            if stored is None:
                raise RecipeBuildError(
                    "build.source_unavailable", "verified source bundle is unavailable"
                )
        try:
            bundle = self._bundles.get(source_sha256)
            policy = enforce_build_source_policy(
                _source_policy_document(document, build, source_sha256), bundle
            )
        except SourceBundleError as error:
            raise RecipeBuildError(error.code, str(error)) from error
        except SourcePolicyError as error:
            finding = error.report.findings[0]
            raise RecipeBuildError(finding.code, finding.detail) from error
        dockerfile_path = build.get("dockerfile") if isinstance(build, dict) else None
        dockerfile_payload = (
            bundle.files.get(dockerfile_path)
            if isinstance(dockerfile_path, str)
            else None
        )
        if dockerfile_payload is None:
            raise RecipeBuildError(
                "build.source_invalid", "recipe Dockerfile authority is unavailable"
            )
        base_images = list(dockerfile_base_images(dockerfile_payload))
        try:
            snapshot = self._inventory.latest(
                builder_node_id, now=now, maximum_age=self._inventory_max_age
            )
        except KeyError as error:
            raise RecipeBuildError(
                "build.inventory_missing", "fresh builder inventory is unavailable"
            ) from error
        if snapshot.stale:
            raise RecipeBuildError(
                "build.inventory_stale", "builder inventory is stale"
            )
        if "recipe.build.v1" not in snapshot.capabilities:
            raise RecipeBuildError(
                "build.capability_missing",
                "builder does not support typed recipe builds",
            )
        if (
            public_network
            and "recipe.build.egress-proxy.v1" not in snapshot.capabilities
        ):
            raise RecipeBuildError(
                "build.network_capability_missing",
                "fresh builder inventory does not prove the hostname-aware build egress boundary",
            )
        resources, security = _canonical_build_resources(revision)
        temporary_bytes = int(resources["temporary_bytes"])
        memory_bytes = int(resources["memory_bytes"])
        cpu_cores = int(resources["cpu_cores"])
        processes = int(resources["processes"])
        capabilities = list(security["capabilities"])
        with self._sessions() as session:
            disk_reserved = _reserved(session, builder_node_id, "disk")
            memory_reserved = _reserved(session, builder_node_id, "host-memory")
        # The rootless builder retains inputs while exporting the image. Treat
        # recipe storage as a generous peak envelope, not an exact quota over
        # Podman's implementation-specific graph. Preserve a separate host
        # reserve so an admitted build cannot crowd out the Spark itself.
        output_bytes = _declared_image_bytes(document)
        base_image_storage_bytes = int(resources["download_bytes"]) if base_images else 0
        disk_envelope = _build_disk_envelope(
            base_image_bytes=base_image_storage_bytes,
            temporary_bytes=temporary_bytes,
            source_bytes=len(bundle.archive),
            output_bytes=output_bytes,
        )
        if (
            snapshot.disk_free_bytes - disk_reserved
            < disk_envelope + _build_disk_reserve(snapshot.disk_total_bytes)
        ):
            raise RecipeBuildError(
                "build.insufficient_disk", "builder lacks temporary disk capacity"
            )
        if snapshot.host_memory_free_bytes - memory_reserved < memory_bytes:
            raise RecipeBuildError(
                "build.insufficient_memory", "builder lacks build memory capacity"
            )
        projected = revision.projected if isinstance(revision.projected, Mapping) else {}
        model_inputs = projected.get("build_model_artifacts")
        topology_inputs = projected.get("build_topology_inputs")
        build_identity = derive_build_input_identity(
            build,
            source_bundle_sha256=source_sha256,
            builder_binary_digest=builder_binary_digest,
            artifact_format=BUILD_ARTIFACT_FORMAT,
            base_images=base_images,
            effective_settings=document.get("settings"),
            topology_inputs=(topology_inputs if isinstance(topology_inputs, Mapping) else None),
            model_artifacts=(model_inputs if isinstance(model_inputs, Sequence) and not isinstance(model_inputs, (str, bytes)) else None),
        )
        build_input_sha256 = _digest(build_identity)
        if resolution is not None:
            intent = copy.deepcopy(build_identity)
            intent.pop("builder_binary_digest", None)
            if (
                resolution.recipe_revision_id != revision.id
                or resolution.recipe_content_sha256 != revision.content_digest
                or resolution.source_bundle_sha256 != source_sha256
                or resolution.input_intent_sha256 != _digest(intent)
            ):
                raise RecipeBuildError(
                    "build.resolution_stale",
                    "immutable build resolution no longer matches the recipe",
                )
        proposed_build_id = str(uuid.uuid4())
        limits = {
            "cpu_cores": cpu_cores,
            "memory_bytes": memory_bytes,
            "temporary_bytes": temporary_bytes,
            "processes": processes,
            "timeout_seconds": int(resources["timeout_seconds"]),
            "output_bytes": output_bytes,
            "gpu": 0,
            "privileged": False,
            "host_mounts": False,
            "container_socket": False,
        }
        payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "recipe.build.v1",
            "build_id": proposed_build_id,
            "recipe_revision_id": revision.id,
            "recipe_content_sha256": revision.content_digest,
            "source_bundle_sha256": source_sha256,
            "source_bundle_bytes": len(bundle.archive),
            "build_input_sha256": build_input_sha256,
            "base_images": copy.deepcopy(base_images),
            "base_image_storage_bytes": base_image_storage_bytes,
            "capabilities": capabilities,
            "dockerfile": build["dockerfile"],
            "platform": _canonical_build_platform(build),
            "arguments": copy.deepcopy(build["arguments"]),
            "network": copy.deepcopy(build["network"]),
            "options": copy.deepcopy(
                projected.get("build_options", {})
                if isinstance(projected.get("build_options", {}), Mapping)
                else {}
            ),
            "limits": limits,
            "target": build.get("target"),
        }
        policy_document = {
            "passed": policy.passed,
            "source_bundle_sha256": policy.source_bundle_sha256,
            "dockerfile": policy.dockerfile,
            "findings": [asdict(item) for item in policy.findings],
            "builder_binary_digest": builder_binary_digest,
            "artifact_format": BUILD_ARTIFACT_FORMAT,
        }
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(RecipeBuild).where(
                    RecipeBuild.recipe_revision_id == revision.id,
                    RecipeBuild.builder_node_id == builder_node_id,
                    RecipeBuild.build_input_sha256 == build_input_sha256,
                )
            )
            if existing is None:
                # Reusable image bytes are keyed by executable inputs, not by
                # editorial recipe provenance.  Only a succeeded receipt may
                # cross a revision boundary; planned/failed rows remain tied
                # to their original authorization and recovery state.
                existing = session.scalar(
                    select(RecipeBuild)
                    .where(
                        RecipeBuild.builder_node_id == builder_node_id,
                        RecipeBuild.build_input_sha256 == build_input_sha256,
                        RecipeBuild.state == "succeeded",
                    )
                    .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
                    .limit(1)
                )
            if existing is None:
                existing = RecipeBuild(
                    id=proposed_build_id,
                    recipe_revision_id=revision.id,
                    builder_node_id=builder_node_id,
                    source_bundle_sha256=source_sha256,
                    build_input_sha256=build_input_sha256,
                    state="planned",
                    policy_report=policy_document,
                    plan=copy.deepcopy(payload),
                    created_at=now,
                    updated_at=now,
                )
                session.add(existing)
                session.flush()
            elif existing.recipe_revision_id == revision.id:
                payload = copy.deepcopy(existing.plan)
            else:
                # Keep the immutable receipt and its original provenance. The
                # plan returned to the caller carries the newly requested
                # recipe digest while execution reuses the exact image result.
                payload["build_id"] = existing.id
                payload["recipe_revision_id"] = revision.id
                payload["recipe_content_sha256"] = revision.content_digest
            build_id = existing.id
        return RecipeBuildPlan(
            build_id=build_id,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            builder_node_id=builder_node_id,
            source_bundle_sha256=source_sha256,
            build_input_sha256=build_input_sha256,
            agent_payload=payload,
        )

    def record_success(
        self,
        build_id: str,
        *,
        build_input_sha256: str,
        image_digest: str,
        oci_layout_sha256: str,
        image_bytes: int,
        now: datetime,
    ) -> CompletedRecipeBuild:
        if (
            _SHA256.fullmatch(build_input_sha256) is None
            or _OCI_DIGEST.fullmatch(image_digest) is None
            or _SHA256.fullmatch(oci_layout_sha256) is None
            or not isinstance(image_bytes, int)
            or isinstance(image_bytes, bool)
            or image_bytes < 1
        ):
            raise RecipeBuildError(
                "build.evidence_invalid", "build result evidence is invalid"
            )
        with self._sessions.begin() as session:
            build = session.get(RecipeBuild, build_id, with_for_update=True)
            if build is None:
                raise KeyError(build_id)
            if build.build_input_sha256 != build_input_sha256:
                raise RecipeBuildError(
                    "build.input_mismatch", "build result does not match its inputs"
                )
            if build.state == "succeeded":
                if (
                    build.image_digest != image_digest
                    or build.oci_layout_sha256 != oci_layout_sha256
                    or build.image_bytes != image_bytes
                ):
                    raise RecipeBuildError(
                        "build.result_conflict", "build already has different evidence"
                    )
            elif build.state not in {"planned", "building"}:
                raise RecipeBuildError(
                    "build.state", "failed build cannot accept success evidence"
                )
            else:
                build.state = "succeeded"
                build.image_digest = image_digest
                build.oci_layout_sha256 = oci_layout_sha256
                build.image_bytes = image_bytes
                build.error = None
                build.updated_at = now
        return CompletedRecipeBuild(
            build_id, image_digest, oci_layout_sha256, image_bytes
        )

    def reserve_in_session(
        self, session: Session, plan: RecipeBuildPlan, *, now: datetime
    ) -> None:
        build = session.get(RecipeBuild, plan.build_id, with_for_update=True)
        revision = (
            session.get(
                CatalogDocumentRevision, plan.recipe_revision_id, with_for_update=True
            )
            if build is not None
            else None
        )
        if (
            revision is None
            or revision.kind != "recipe"
            or revision.state != "active"
            or revision.content_digest != plan.recipe_content_sha256
        ):
            raise RecipeBuildError(
                "build.dependencies_stale", "exact recipe dependencies changed"
            )
        expected_binary_digest = (
            build.policy_report.get("builder_binary_digest")
            if build is not None and isinstance(build.policy_report, dict)
            else None
        )
        expected_format = (
            build.policy_report.get("artifact_format")
            if build is not None and isinstance(build.policy_report, dict)
            else None
        )
        if (
            build is None
            or build.builder_node_id != plan.builder_node_id
            or build.build_input_sha256 != plan.build_input_sha256
            or expected_format != BUILD_ARTIFACT_FORMAT
        ):
            raise RecipeBuildError(
                "build.plan_invalid", "stored build identity is invalid"
            )
        try:
            snapshot = self._inventory.latest(
                plan.builder_node_id, now=now, maximum_age=self._inventory_max_age
            )
        except KeyError as error:
            raise RecipeBuildError(
                "build.inventory_missing", "fresh builder inventory is unavailable"
            ) from error
        node = session.get(AgentNode, plan.builder_node_id, with_for_update=True)
        if node is None:
            raise RecipeBuildError("build.node_unknown", "builder GPU node is unknown")
        _validate_builder(node)
        if node.binary_digest != expected_binary_digest:
            raise RecipeBuildError(
                "build.runtime_changed", "builder runtime identity changed"
            )
        if snapshot.stale:
            raise RecipeBuildError(
                "build.inventory_stale", "builder inventory is stale"
            )
        limits = plan.agent_payload.get("limits")
        source_bytes = plan.agent_payload.get("source_bundle_bytes")
        if not isinstance(limits, dict) or not isinstance(source_bytes, int):
            raise RecipeBuildError("build.plan_invalid", "build plan is invalid")
        temporary_bytes = limits.get("temporary_bytes")
        memory_bytes = limits.get("memory_bytes")
        output_bytes = limits.get("output_bytes")
        base_image_storage_bytes = plan.agent_payload.get("base_image_storage_bytes")
        if (
            not isinstance(temporary_bytes, int)
            or not isinstance(memory_bytes, int)
            or not isinstance(output_bytes, int)
            or not isinstance(base_image_storage_bytes, int)
        ):
            raise RecipeBuildError("build.plan_invalid", "build plan is invalid")
        disk_bytes = _build_disk_envelope(
            base_image_bytes=base_image_storage_bytes,
            temporary_bytes=temporary_bytes,
            source_bytes=source_bytes,
            output_bytes=output_bytes,
        )
        if snapshot.disk_free_bytes - _reserved(
            session, plan.builder_node_id, "disk"
        ) < disk_bytes + _build_disk_reserve(snapshot.disk_total_bytes):
            raise RecipeBuildError(
                "build.insufficient_disk", "builder disk capacity changed"
            )
        if (
            snapshot.host_memory_free_bytes
            - _reserved(session, plan.builder_node_id, "host-memory")
            < memory_bytes
        ):
            raise RecipeBuildError(
                "build.insufficient_memory", "builder memory capacity changed"
            )
        session.add_all(
            (
                ResourceReservation(
                    node_id=plan.builder_node_id,
                    kind="disk",
                    resource_key=plan.build_input_sha256,
                    amount_bytes=disk_bytes,
                    owner_kind="recipe-build",
                    owner_id=plan.build_id,
                    state="active",
                    plan_digest=plan.build_input_sha256,
                    created_at=now,
                ),
                ResourceReservation(
                    node_id=plan.builder_node_id,
                    kind="host-memory",
                    resource_key=plan.build_input_sha256,
                    amount_bytes=memory_bytes,
                    owner_kind="recipe-build",
                    owner_id=plan.build_id,
                    state="active",
                    plan_digest=plan.build_input_sha256,
                    created_at=now,
                ),
            )
        )

    def plan_distribution(
        self, build_id: str, mapping_id: str, *, generation: int
    ) -> ImageDistributionPlan:
        with self._sessions() as session:
            build = session.get(RecipeBuild, build_id)
            mapping = session.get(ClusterMapping, mapping_id)
            if build is None:
                raise KeyError(build_id)
            if mapping is None:
                raise KeyError(mapping_id)
            if (
                build.state != "succeeded"
                or build.image_digest is None
                or build.oci_layout_sha256 is None
                or build.image_bytes is None
            ):
                raise RecipeBuildError(
                    "build.result_unavailable", "successful OCI build is unavailable"
                )
            if (
                mapping.state != "ready"
                or mapping.generation != generation
                or mapping.recipe_revision_id != build.recipe_revision_id
            ):
                raise RecipeBuildError(
                    "build.mapping_mismatch",
                    "mapping generation does not match the build",
                )
            nodes = tuple(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id == mapping_id)
                    .order_by(ClusterMappingNode.rank)
                )
            )
            targets: list[tuple[str, dict[str, object]]] = []
            for item in nodes:
                # A durable artifact row records accepted evidence, not current
                # Docker cache state. Re-importing the immutable layout makes a
                # new mapping self-healing after image pruning or runtime changes.
                node = session.get(AgentNode, item.node_id)
                if (
                    node is None
                    or node.state != "active"
                    or "recipe.image.import.v1" not in node.capabilities
                ):
                    raise RecipeBuildError(
                        "build.import_capability_missing",
                        "a mapped GPU node cannot import the exact OCI result",
                    )
                targets.append(
                    (
                        item.node_id,
                        {
                            "schema_version": 1,
                            "kind": "recipe.image.import.v1",
                            "build_id": build.id,
                            "mapping_id": mapping.id,
                            "mapping_generation": mapping.generation,
                            "source_node_id": build.builder_node_id,
                            "image_digest": build.image_digest,
                            "oci_layout_sha256": build.oci_layout_sha256,
                            "image_bytes": build.image_bytes,
                        },
                    )
                )
        return ImageDistributionPlan(
            build.id,
            mapping.id,
            mapping.generation,
            build.image_digest,
            tuple(targets),
        )


def _validate_builder(node: AgentNode) -> None:
    if (
        node.state != "active"
        or node.revoked_at is not None
        or node.architecture != "linux-arm64"
        or not isinstance(node.binary_digest, str)
        or _SHA256.fullmatch(node.binary_digest) is None
        or "recipe.build.v1" not in node.capabilities
    ):
        raise RecipeBuildError(
            "build.node_incompatible", "builder GPU node is inactive or incompatible"
        )


def _public_build_network(build: object) -> bool:
    if not isinstance(build, dict):
        return False
    network = build.get("network")
    return isinstance(network, dict) and network.get("mode") == "public"


def _declared_image_bytes(document: dict[str, object]) -> int:
    values: list[int] = []
    try:
        topology = recipe_topology(document)
    except RecipeContractError:
        topology = {}
    roles = topology.get("roles")
    if isinstance(roles, list):
        for role in roles:
            if not isinstance(role, dict):
                continue
            resources = role.get("resources")
            disk = resources.get("disk") if isinstance(resources, dict) else None
            image_bytes = disk.get("image_bytes") if isinstance(disk, dict) else None
            if isinstance(image_bytes, int) and not isinstance(image_bytes, bool):
                values.append(image_bytes)
    if not values or min(values) < 1 or max(values) > 16 * 1024**4:
        raise RecipeBuildError(
            "build.image_size_invalid",
            "recipe topology must declare a positive per-node image size",
        )
    return max(values)


def _reserved(session: Session, node_id: str, kind: str) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(ResourceReservation.amount_bytes), 0)).where(
                ResourceReservation.node_id == node_id,
                ResourceReservation.kind == kind,
                ResourceReservation.state == "active",
            )
        )
        or 0
    )


def _valid_succeeded_receipt(build: RecipeBuild) -> bool:
    """Require complete immutable evidence before considering a cache hit."""
    return (
        build.state == "succeeded"
        and _OCI_DIGEST.fullmatch(build.image_digest or "") is not None
        and _SHA256.fullmatch(build.oci_layout_sha256 or "") is not None
        and isinstance(build.image_bytes, int)
        and not isinstance(build.image_bytes, bool)
        and build.image_bytes > 0
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
