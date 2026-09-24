"""Durable source-build planning and exact OCI result recording."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Protocol

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_forge_contracts import RecipeDefinition
from vonk_forge_contracts.recipe import RecipeSetting, RecipeSettings

from .admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    acquire_admission_keys,
    lock_admission_rows,
    node_admission_key,
)
from .catalog_revision_contract import (
    BuildModelArtifactProjection,
    BuildResourcesProjection,
    BuildSecurityProjection,
    CatalogRevisionContractError,
    RecipeRevisionProjection,
    read_catalog_projection,
)
from .disk_reservations import outstanding_disk_reservation_bytes
from .inventory_repository import InventoryRepository, InventorySnapshotView
from .memory_reservations import memory_reservations, memory_reserve_floor
from .models import (
    AgentNode,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    RecipeBuild,
    RecipeSourceBundle,
    ResourceReservation,
)
from .profile_capacity import profile_build_memory_claims
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    build_plan_document,
    build_policy_document,
    parse_stored_build_plan,
    parse_stored_build_policy,
)
from .recipe_runtime_specs import RecipeRuntimeSpecError, recipe_topology
from .resource_planning import memory_capacity_snapshot
from .runtime_adapters import (
    RuntimeAdapter,
    RuntimeAdapterError,
    resolve_runtime_adapter,
)
from .runtime_image_preparation import (
    RuntimeImageReceipt,
    require_runtime_image_authorization,
)
from .source_bundles import SourceBundleError, SourceBundleStoreProtocol
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
# Controller source builds are always linux/arm64 under the v1 runtime
# contract, so the filesystem build lookup is scoped by the same identity.
_BUILD_RUNTIME_PLATFORM = "linux/arm64"
_BUILD_RUNTIME_INTERFACE = "vonk.runtime.v1"
_RECIPE_SETTINGS = TypeAdapter(RecipeSettings)


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
    runtime_adapter: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Project the exact executable inputs used to build a recipe image.

    Catalog revision/content digests authorize and describe a request.  They
    are deliberately absent from this identity: changing notes, capability
    evidence, or provenance must not rebuild an unchanged executable.  Model
    selectors likewise contribute their canonical file content only when the
    caller says the build consumes those files; roles and mounts belong to the
    runtime execution identity.

    The resolved runtime adapter is executable input, not provenance: the
    platform adaptation stage runs as part of producing the final image, so an
    adapter change must invalidate a prepared image that embeds it.
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
    if runtime_adapter is not None:
        identity["runtime_adapter"] = copy.deepcopy(dict(runtime_adapter))
    return identity


def _resolved_adapter(projected: RecipeRevisionProjection) -> RuntimeAdapter:
    """Resolve the platform adaptation for a recipe or fail closed."""
    try:
        return resolve_runtime_adapter(
            projected.runtime_engine, projected.topology.model_dump(mode="json")
        )
    except RuntimeAdapterError as error:
        raise RecipeBuildError("build.adapter_unavailable", str(error)) from error


def _build_effective_settings(value: object | None) -> dict[str, object] | None:
    """Project rebuild inputs from the shared current RecipeSettings contract."""
    if value is None:
        return None
    settings = _RECIPE_SETTINGS.validate_json(canonical_message(value))
    selected = {
        name: copy.deepcopy(setting.value)
        for name in type(settings).model_fields
        if isinstance(setting := getattr(settings, name), RecipeSetting)
        and setting.change_effect == "rebuild"
    }
    selected.update(
        {
            f"knobs.{name}": copy.deepcopy(setting.value)
            for name, setting in settings.knobs.items()
            if setting.change_effect == "rebuild"
        }
    )
    return (
        {"values": selected, "change_effects": {name: "rebuild" for name in selected}}
        if selected
        else None
    )


def _canonical_recipe_document(value: object) -> dict[str, object]:
    try:
        recipe = RecipeDefinition.model_validate_json(canonical_message(value))
    except (TypeError, ValueError) as error:
        raise RecipeBuildError(
            "build.contract_invalid", "stored recipe does not satisfy RecipeDefinition"
        ) from error
    return recipe.model_dump(mode="json")


def _canonical_model_build_inputs(
    artifacts: Sequence[object],
) -> list[dict[str, object]]:
    projected: list[dict[str, object]] = []
    for artifact in artifacts:
        if isinstance(artifact, BuildModelArtifactProjection):
            path = artifact.path
            digest = artifact.sha256
            size = artifact.size_bytes
        elif isinstance(artifact, Mapping):
            path = artifact.get("path")
            digest = artifact.get("sha256")
            size = artifact.get(
                "download_bytes", artifact.get("size_bytes", artifact.get("size"))
            )
        else:
            path = getattr(artifact, "path", None)
            digest = getattr(artifact, "sha256", None)
            size = getattr(
                artifact,
                "download_bytes",
                getattr(artifact, "size_bytes", getattr(artifact, "size", None)),
            )
        if (
            not isinstance(path, str)
            or not isinstance(digest, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
        ):
            raise TypeError(
                "model build inputs require canonical path, sha256, and size"
            )
        projected.append({"path": path, "sha256": digest, "download_bytes": size})
    return sorted(
        projected,
        key=lambda item: (
            str(item["path"]),
            str(item["sha256"]),
            item["download_bytes"],
        ),
    )


def _canonical_build(
    document: Mapping[str, object], projected: RecipeRevisionProjection | None = None
) -> Mapping[str, object]:
    execution = document.get("execution")
    if not isinstance(execution, Mapping) or execution.get("mode") != "build":
        raise RecipeBuildError("build.not_required", "recipe selects a prebuilt image")
    build = execution.get("build")
    if not isinstance(build, Mapping):
        raise RecipeBuildError(
            "build.contract_invalid", "canonical execution.build is unavailable"
        )
    compiled = {**build, "dockerfile": _bundle_dockerfile_path(build)}
    if projected is not None:
        # Executable platform policy participates in the same cache identity
        # as the Dockerfile and base images. Resource quotas do not.
        compiled["options"] = (
            projected.build_options.model_dump(mode="json")
            if projected.build_options is not None
            else {}
        )
        compiled["security"] = (
            projected.build_security.model_dump(mode="json")
            if projected.build_security is not None
            else {}
        )
    return compiled


def _bundle_dockerfile_path(build: Mapping[str, object]) -> object:
    """Project the repository path into the verified build-context archive."""

    context = build.get("context")
    path = context.get("path") if isinstance(context, Mapping) else None
    dockerfile = build.get("dockerfile")
    if isinstance(path, str) and isinstance(dockerfile, str):
        return dockerfile.removeprefix(path.rstrip("/") + "/")
    return dockerfile


def _source_bundle_handle(projected: RecipeRevisionProjection) -> str:
    """Return the catalog-owned source package digest for a build.

    The package handle is a catalog projection.  A recipe document cannot
    smuggle source bytes or an arbitrary URL into the builder.
    """
    candidate = projected.source_bundle_sha256
    if candidate is None or _SHA256.fullmatch(candidate) is None:
        raise RecipeBuildError(
            "build.source_unavailable", "catalog package handle is unavailable"
        )
    return candidate


def _canonical_build_resources(
    projected: RecipeRevisionProjection,
) -> tuple[BuildResourcesProjection, BuildSecurityProjection]:
    """Read compiler-owned build admission projections from the catalog row."""
    resources = projected.build_resources
    security = projected.build_security
    if resources is None:
        raise RecipeBuildError(
            "build.resources_invalid",
            "canonical runtime compiler did not publish a build resource envelope",
        )
    if security is None:
        raise RecipeBuildError(
            "build.security_invalid",
            "canonical runtime compiler did not publish a build security envelope",
        )
    if any(not isinstance(item, str) or not item for item in security.capabilities):
        raise RecipeBuildError(
            "build.security_invalid", "canonical build capabilities are invalid"
        )
    return resources, security


def _canonical_build_platform(build: Mapping[str, object]) -> str:
    base_image = build.get("base_image")
    if (
        not isinstance(base_image, Mapping)
        or base_image.get("platform") != "linux/arm64"
    ):
        raise RecipeBuildError(
            "build.platform_invalid",
            "canonical source builds require a linux/arm64 base image",
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
        raise RecipeBuildError(
            "build.source_invalid", "canonical build context path is invalid"
        )
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


class RecipeBuildAdmissionBusy(RecipeBuildError):
    code = "build.capacity_busy"

    def __init__(self) -> None:
        super().__init__(self.code, "builder capacity writer is busy")


def _read_recipe_projection(
    revision: CatalogDocumentRevision,
) -> RecipeRevisionProjection:
    try:
        projected = read_catalog_projection(revision)
    except CatalogRevisionContractError as error:
        raise RecipeBuildError(
            "build.contract_invalid",
            "stored recipe catalog projection is invalid",
        ) from error
    if not isinstance(projected, RecipeRevisionProjection):
        raise RecipeBuildError(
            "build.contract_invalid",
            "stored recipe catalog projection is unavailable",
        )
    return projected


@dataclass(frozen=True, slots=True)
class RecipeBuildPlan:
    build_id: str
    recipe_revision_id: str
    recipe_content_sha256: str
    builder_node_id: str
    source_bundle_sha256: str
    build_input_sha256: str
    agent_payload: dict[str, object]
    policy_report: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class RecipeBuildResolution:
    """Builder-independent source-build resolution.

    ``input_intent_sha256`` is an immutable request identity, not an
    executable build identity.  ``build_input_sha256`` is populated only
    when a succeeded receipt was found and verified with its recorded
    builder binary digest.  The selected live builder must still be admitted
    and planned before a new final build identity is usable for dispatch.
    ``stale_receipt`` reports that SQL recorded a succeeded build for this
    exact identity but no verified archive is present on disk; a fresh build
    must replace it rather than replay the vanished result.  ``receipt_pending``
    reports the opposite, recoverable case: the verified archive is present but
    its verification receipt is absent or incomplete, so preparation must
    re-verify the bytes and republish the receipt instead of building again.
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
    stale_receipt: bool = False
    receipt_pending: bool = False

    @property
    def cached(self) -> bool:
        return self.build_id is not None

    def build_input_for_builder(self, binary_digest: str) -> str:
        """Bind canonical executable intent to an accepted builder identity."""
        if _SHA256.fullmatch(binary_digest) is None:
            raise RecipeBuildError(
                "build.plan_invalid", "recorded builder identity is invalid"
            )
        return _digest(self.input_intent | {"builder_binary_digest": binary_digest})


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


@dataclass(frozen=True, slots=True)
class _BuildCandidate:
    """Plain snapshot of one succeeded build row, safe to use after commit.

    Managed storage is consulted only after the reading transaction has ended,
    so a candidate carries the values that decision needs instead of keeping an
    ORM instance alive across storage I/O.
    """

    build_id: str
    builder_node_id: str
    build_input_sha256: str
    builder_binary_digest: str
    image_digest: str
    oci_layout_sha256: str
    image_bytes: int


class PreparedBuildReceipt(Protocol):
    """Verified filesystem identity of one prepared Controller build."""

    build_id: str | None
    build_input_sha256: str | None
    image_digest: str
    oci_archive_sha256: str
    image_bytes: int


class PreparedBuildLookup(Protocol):
    """Answer whether exact build bytes are already prepared on disk.

    The lookup is keyed by the executable build input identity the receipt
    recorded beside the archive. It is the reuse owner, so it never consults the
    SQL build index and never falls back to a weaker key.
    """

    def __call__(
        self,
        build_input_sha256: str,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        expected_archive_sha256: str | None = None,
    ) -> PreparedBuildReceipt | None: ...


class RecipeBuildService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        bundles: SourceBundleStoreProtocol,
        inventory_max_age: int = 300,
        build_archive_available: Callable[[str, int], bool] | None = None,
        prepared_builds: PreparedBuildLookup | None = None,
    ) -> None:
        self._sessions = sessions
        self._bundles = bundles
        self._inventory = InventoryRepository(sessions)
        self._inventory_max_age = inventory_max_age
        self._build_archive_available = build_archive_available
        self._prepared_builds = prepared_builds

    def _stored_archive_present(self, archive_sha256: str, image_bytes: int) -> bool:
        """Cheap presence/type/length check usable inside a short transaction."""

        if self._build_archive_available is None:
            return True
        return self._build_archive_available(archive_sha256, image_bytes)

    def _succeeded_build_available(self, build: RecipeBuild) -> bool:
        if not _valid_succeeded_receipt(build):
            return False
        assert build.oci_layout_sha256 is not None
        assert build.image_bytes is not None
        return self._stored_archive_present(build.oci_layout_sha256, build.image_bytes)

    def check_source(self, recipe_revision_id: str) -> SourcePolicyReport:
        with self._sessions() as session:
            revision = session.get(CatalogDocumentRevision, recipe_revision_id)
            if revision is None:
                raise KeyError(recipe_revision_id)
            if revision.kind != "recipe" or revision.state != "active":
                raise RecipeBuildError(
                    "build.recipe_unresolved", "only a resolved recipe can be checked"
                )
            projected = _read_recipe_projection(revision)
            document = _canonical_recipe_document(revision.document)
            build = _canonical_build(document, projected)
            source_sha256 = _source_bundle_handle(projected)
            if session.get(RecipeSourceBundle, source_sha256) is None:
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
            projected = _read_recipe_projection(revision)
            document = _canonical_recipe_document(revision.document)
            build = _canonical_build(document, projected)
            adapter = _resolved_adapter(projected)
            source_sha256 = _source_bundle_handle(projected)
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
        _canonical_build_resources(projected)
        _canonical_build_platform(build)
        _declared_image_bytes(document)
        model_inputs = projected.build_model_artifacts
        topology_inputs = projected.build_topology_inputs
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
            effective_settings=document["settings"],
            topology_inputs=topology,
            model_artifacts=model_artifacts,
            runtime_adapter=adapter.document(),
        )
        intent_sha256 = _digest(intent)
        resolution = RecipeBuildResolution(
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            source_bundle_sha256=source_sha256,
            input_intent_sha256=intent_sha256,
            input_intent=copy.deepcopy(intent),
        )

        # Read a bounded snapshot and commit before touching managed storage:
        # a database transaction contains database work only.
        candidates: list[_BuildCandidate] = []
        with self._sessions() as session:
            rows = session.scalars(
                select(RecipeBuild)
                .where(
                    RecipeBuild.source_bundle_sha256 == source_sha256,
                    RecipeBuild.image_digest.is_not(None),
                )
                .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
            )
            for candidate in rows:
                # A replacement attempt changes execution state, not the last
                # verified artifact. Managed storage below decides whether
                # that exact receipt is still usable.
                if not _has_build_receipt(candidate):
                    continue
                try:
                    report = parse_stored_build_policy(candidate.policy_report)
                    parse_stored_build_plan(candidate.plan)
                except RecipeExecutionContractError:
                    continue
                builder_digest = report.builder_binary_digest
                if (
                    not isinstance(builder_digest, str)
                    or _SHA256.fullmatch(builder_digest) is None
                    or report.artifact_format != BUILD_ARTIFACT_FORMAT
                    or report.source_bundle_sha256 != source_sha256
                ):
                    continue
                if candidate.build_input_sha256 != resolution.build_input_for_builder(
                    builder_digest
                ):
                    continue
                assert candidate.image_digest is not None
                assert candidate.oci_layout_sha256 is not None
                assert candidate.image_bytes is not None
                candidates.append(
                    _BuildCandidate(
                        build_id=candidate.id,
                        builder_node_id=candidate.builder_node_id,
                        build_input_sha256=candidate.build_input_sha256,
                        builder_binary_digest=builder_digest,
                        image_digest=candidate.image_digest,
                        oci_layout_sha256=candidate.oci_layout_sha256,
                        image_bytes=candidate.image_bytes,
                    )
                )

        cached: _BuildCandidate | None = None
        prepared: PreparedBuildReceipt | None = None
        receipt_pending = False
        stale_receipt = False
        for candidate in candidates:
            # The file on disk, not the SQL row, decides availability. A
            # present archive whose verification receipt is absent or
            # incomplete is a metadata gap that preparation repairs by
            # re-verifying the bytes; only missing bytes are cache loss that
            # forces a rebuild. Trusting the SQL row here would make the row a
            # second availability authority.
            if not self._stored_archive_present(
                candidate.oci_layout_sha256, candidate.image_bytes
            ):
                stale_receipt = True
                continue
            receipt = None
            if self._prepared_builds is not None:
                receipt = self._prepared_builds(
                    candidate.build_input_sha256,
                    expected_architecture=_BUILD_RUNTIME_PLATFORM,
                    expected_runtime_interface=_BUILD_RUNTIME_INTERFACE,
                )
            cached = candidate
            prepared = receipt
            receipt_pending = receipt is None
            stale_receipt = False
            break

        if cached is None:
            return replace(resolution, stale_receipt=stale_receipt)
        if prepared is not None:
            build_id = prepared.build_id or cached.build_id
            build_input_sha256 = prepared.build_input_sha256
            image_digest = prepared.image_digest
            oci_layout_sha256 = prepared.oci_archive_sha256
            image_bytes = prepared.image_bytes
        else:
            build_id = cached.build_id
            build_input_sha256 = cached.build_input_sha256
            image_digest = cached.image_digest
            oci_layout_sha256 = cached.oci_layout_sha256
            image_bytes = cached.image_bytes
        if (
            not isinstance(build_input_sha256, str)
            or not isinstance(image_digest, str)
            or not isinstance(oci_layout_sha256, str)
            or not isinstance(image_bytes, int)
            or isinstance(image_bytes, bool)
        ):
            raise RecipeBuildError(
                "build.plan_invalid", "cached source build receipt is incomplete"
            )
        return replace(
            resolution,
            build_input_sha256=build_input_sha256,
            build_id=build_id,
            builder_node_id=cached.builder_node_id,
            builder_binary_digest=cached.builder_binary_digest,
            image_digest=image_digest,
            oci_layout_sha256=oci_layout_sha256,
            image_bytes=image_bytes,
            receipt_pending=receipt_pending,
        )

    def prepare_plan(
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
            projected = _read_recipe_projection(revision)
            node = session.get(AgentNode, builder_node_id)
            if node is None:
                raise RecipeBuildError(
                    "build.node_unknown", "builder GPU node is unknown"
                )
            _validate_builder(node)
            document = _canonical_recipe_document(revision.document)
            build = _canonical_build(document, projected)
            adapter = _resolved_adapter(projected)
            source_sha256 = _source_bundle_handle(projected)
            public_network = _public_build_network(build)
            # Claim capabilities describe operations; the probed egress boundary
            # is checked against fresh host inventory below.
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
        resources, security = _canonical_build_resources(projected)
        temporary_bytes = resources.temporary_bytes
        memory_bytes = resources.memory_bytes
        cpu_cores = resources.cpu_cores
        processes = resources.processes
        capabilities = list(security.capabilities)
        with self._sessions() as session:
            disk_reserved = outstanding_disk_reservation_bytes(
                session, builder_node_id, inventory_observed_at=snapshot.observed_at
            )
            memory_available = _available_build_memory(session, snapshot)
        # The rootless builder retains inputs while exporting the image. Treat
        # recipe storage as a generous peak envelope, not an exact quota over
        # Podman's implementation-specific graph. Preserve a separate host
        # reserve so an admitted build cannot crowd out the Spark itself.
        output_bytes = _declared_image_bytes(document)
        base_image_storage_bytes = resources.download_bytes if base_images else 0
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
        if memory_available < memory_bytes:
            raise RecipeBuildError(
                "build.insufficient_memory", "builder lacks build memory capacity"
            )
        model_inputs = projected.build_model_artifacts
        topology_inputs = projected.build_topology_inputs
        build_identity = derive_build_input_identity(
            build,
            source_bundle_sha256=source_sha256,
            builder_binary_digest=builder_binary_digest,
            artifact_format=BUILD_ARTIFACT_FORMAT,
            base_images=base_images,
            effective_settings=document["settings"],
            topology_inputs=(
                topology_inputs if isinstance(topology_inputs, Mapping) else None
            ),
            model_artifacts=(
                model_inputs
                if isinstance(model_inputs, Sequence)
                and not isinstance(model_inputs, (str, bytes))
                else None
            ),
            runtime_adapter=adapter.document(),
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
            "timeout_seconds": resources.timeout_seconds,
            "output_bytes": output_bytes,
            "gpu": 0,
            "privileged": False,
            "host_mounts": False,
            "container_socket": False,
        }
        payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "recipe.build.v1",
            "adapter": adapter.to_wire().model_dump(mode="json"),
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
            "options": (
                projected.build_options.model_dump(mode="json")
                if projected.build_options is not None
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
        try:
            # Persist the canonical JSON-mode representation.  This is also
            # the representation handed to the agent build queue.
            payload = build_plan_document(payload)
            policy_document = build_policy_document(policy_document)
        except RecipeExecutionContractError as error:
            raise RecipeBuildError(
                "build.contract_invalid", "source build envelope is invalid"
            ) from error
        return RecipeBuildPlan(
            build_id=proposed_build_id,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            builder_node_id=builder_node_id,
            source_bundle_sha256=source_sha256,
            build_input_sha256=build_input_sha256,
            agent_payload=payload,
            policy_report=policy_document,
        )

    def plan(
        self,
        recipe_revision_id: str,
        builder_node_id: str,
        *,
        now: datetime,
        resolution: RecipeBuildResolution | None = None,
    ) -> RecipeBuildPlan:
        """Prepare a build outside locks, then persist it in one short transaction."""
        prepared = self.prepare_plan(
            recipe_revision_id,
            builder_node_id,
            now=now,
            resolution=resolution,
        )
        with self._sessions.begin() as session:
            return self.persist_plan_in_session(session, prepared, now=now)

    def persist_plan_in_session(
        self, session: Session, plan: RecipeBuildPlan, *, now: datetime
    ) -> RecipeBuildPlan:
        """Persist a prepared plan using the caller's transaction.

        This helper intentionally never opens another transaction.  It may be
        called while the availability parent and builder rows are locked.
        """
        try:
            acquire_admission_keys(
                session, (node_admission_key(plan.builder_node_id),)
            )
            locked = lock_admission_rows(
                session,
                (
                    AdmissionRowLock(
                        "build-builder-node",
                        AgentNode,
                        select(AgentNode).where(
                            AgentNode.node_id == plan.builder_node_id
                        ),
                    ),
                ),
            )
            node = next(iter(locked["build-builder-node"]), None)
        except AdmissionLockBusy as error:
            raise RecipeBuildAdmissionBusy() from error
        if node is None:
            raise RecipeBuildError("build.node_unknown", "builder GPU node is unknown")
        _validate_builder(node)
        policy_document = plan.policy_report
        if not isinstance(policy_document, dict):
            raise RecipeBuildError(
                "build.plan_invalid", "prepared source build policy is unavailable"
            )
        try:
            policy = parse_stored_build_policy(policy_document)
        except RecipeExecutionContractError as error:
            raise RecipeBuildError(
                "build.plan_invalid",
                "prepared source build policy is invalid" + error.detail,
            ) from error
        if policy.builder_binary_digest != node.binary_digest:
            raise RecipeBuildError(
                "build.runtime_changed", "builder runtime identity changed"
            )
        existing = session.scalar(
            select(RecipeBuild).where(
                RecipeBuild.recipe_revision_id == plan.recipe_revision_id,
                RecipeBuild.builder_node_id == plan.builder_node_id,
                RecipeBuild.build_input_sha256 == plan.build_input_sha256,
            )
        )
        if (
            existing is not None
            and existing.state == "succeeded"
            and _valid_succeeded_receipt(existing)
            and not self._succeeded_build_available(existing)
        ):
            _reopen_build_attempt(existing, now=now)
        if existing is None:
            # Reusable image bytes are keyed by executable inputs, not by
            # editorial recipe provenance. Only a succeeded receipt may cross
            # a revision boundary.
            candidates = session.scalars(
                select(RecipeBuild)
                .where(
                    RecipeBuild.builder_node_id == plan.builder_node_id,
                    RecipeBuild.build_input_sha256 == plan.build_input_sha256,
                    RecipeBuild.state == "succeeded",
                )
                .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
            )
            existing = next(
                (
                    candidate
                    for candidate in candidates
                    if self._succeeded_build_available(candidate)
                ),
                None,
            )
        payload = build_plan_document(copy.deepcopy(plan.agent_payload))
        if existing is None:
            existing = RecipeBuild(
                id=plan.build_id,
                recipe_revision_id=plan.recipe_revision_id,
                builder_node_id=plan.builder_node_id,
                source_bundle_sha256=plan.source_bundle_sha256,
                build_input_sha256=plan.build_input_sha256,
                state="planned",
                policy_report=copy.deepcopy(policy_document),
                plan=copy.deepcopy(payload),
                created_at=now,
                updated_at=now,
            )
            session.add(existing)
            session.flush()
        elif existing.recipe_revision_id == plan.recipe_revision_id:
            try:
                payload = build_plan_document(existing.plan)
                parse_stored_build_policy(existing.policy_report)
            except RecipeExecutionContractError as error:
                if existing.state == "building":
                    # An in-flight attempt owns this row, so its stored
                    # envelope is not stale metadata this planner may rewrite
                    # underneath it.  Fail closed and name the bad field.
                    raise RecipeBuildError(
                        "build.plan_invalid",
                        "stored source build envelope is invalid" + error.detail,
                    ) from error
                # The stored envelope no longer satisfies the current contract
                # (an engine marker an older removal path wrote, or a field an
                # older Controller wrote).  That makes this row unusable
                # history, not a barrier for the build the operator asked for.
                # Replace the damaged documents with the freshly prepared and
                # already-validated envelope bound to this row's own build
                # identity, and clear the stale result so a fresh attempt can
                # start from current bytes rather than the damaged document.
                payload["build_id"] = existing.id
                existing.plan = copy.deepcopy(payload)
                existing.policy_report = copy.deepcopy(policy_document)
                _reopen_build_attempt(existing, now=now)
            else:
                if existing.state == "failed":
                    # A cancelled or failed attempt must not block the rebuild
                    # the operator asked for.  The stored envelope is still
                    # exact, so keep it and return the row to a clean planned
                    # attempt rather than reusing a terminal row.
                    _reopen_build_attempt(existing, now=now)
        else:
            payload["build_id"] = existing.id
            payload["recipe_revision_id"] = plan.recipe_revision_id
            payload["recipe_content_sha256"] = plan.recipe_content_sha256
        try:
            payload = build_plan_document(payload)
        except RecipeExecutionContractError as error:
            raise RecipeBuildError(
                "build.plan_invalid",
                "stored source build plan is invalid" + error.detail,
            ) from error
        return RecipeBuildPlan(
            build_id=existing.id,
            recipe_revision_id=plan.recipe_revision_id,
            recipe_content_sha256=plan.recipe_content_sha256,
            builder_node_id=plan.builder_node_id,
            source_bundle_sha256=plan.source_bundle_sha256,
            build_input_sha256=plan.build_input_sha256,
            agent_payload=payload,
            policy_report=copy.deepcopy(policy_document),
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
        self,
        session: Session,
        plan: RecipeBuildPlan,
        *,
        now: datetime,
        request_id: str | None = None,
    ) -> None:
        try:
            acquire_admission_keys(
                session, (node_admission_key(plan.builder_node_id),)
            )
            self._reserve_in_session(session, plan, now=now, request_id=request_id)
        except AdmissionLockBusy as error:
            raise RecipeBuildAdmissionBusy() from error
        except RecipeBuildError:
            raise
        except ValueError as error:
            raise RecipeBuildError(
                "build.capacity_contract_invalid", str(error)
            ) from error
        except OperationalError as error:
            code = getattr(error.orig, "sqlstate", None) or getattr(
                error.orig, "pgcode", None
            )
            if code in {"55P03", "40P01", "40001", "57014"}:
                raise RecipeBuildAdmissionBusy() from error
            raise

    def _reserve_in_session(
        self,
        session: Session,
        plan: RecipeBuildPlan,
        *,
        now: datetime,
        request_id: str | None,
    ) -> None:
        locked = lock_admission_rows(
            session,
            (
                AdmissionRowLock(
                    "build-builder-node",
                    AgentNode,
                    select(AgentNode).where(
                        AgentNode.node_id == plan.builder_node_id
                    ),
                ),
                AdmissionRowLock(
                    "build-recipe-revision",
                    CatalogDocumentRevision,
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.id == plan.recipe_revision_id
                    ),
                ),
                AdmissionRowLock(
                    "build-recipe-build",
                    RecipeBuild,
                    select(RecipeBuild).where(RecipeBuild.id == plan.build_id),
                ),
            ),
        )
        node = next(iter(locked["build-builder-node"]), None)
        revision = next(iter(locked["build-recipe-revision"]), None)
        build = next(iter(locked["build-recipe-build"]), None)
        if (
            revision is None
            or revision.kind != "recipe"
            or revision.state != "active"
            or revision.content_digest != plan.recipe_content_sha256
        ):
            raise RecipeBuildError(
                "build.dependencies_stale", "exact recipe dependencies changed"
            )
        if build is None:
            raise RecipeBuildError(
                "build.plan_invalid", "stored build identity is invalid"
            )
        try:
            stored_policy = parse_stored_build_policy(build.policy_report)
            parse_stored_build_plan(build.plan)
            requested_plan = parse_stored_build_plan(plan.agent_payload)
        except RecipeExecutionContractError as error:
            raise RecipeBuildError(
                "build.plan_invalid",
                "stored source build envelope is invalid" + error.detail,
            ) from error
        expected_binary_digest = stored_policy.builder_binary_digest
        expected_format = stored_policy.artifact_format
        if (
            build.builder_node_id != plan.builder_node_id
            or build.build_input_sha256 != plan.build_input_sha256
            or expected_format != BUILD_ARTIFACT_FORMAT
            or requested_plan.build_id != plan.build_id
            or requested_plan.build_input_sha256 != plan.build_input_sha256
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
        plan_payload = build_plan_document(requested_plan)
        limits = plan_payload.get("limits")
        source_bytes = plan_payload.get("source_bundle_bytes")
        if not isinstance(limits, dict) or not isinstance(source_bytes, int):
            raise RecipeBuildError("build.plan_invalid", "build plan is invalid")
        temporary_bytes = limits.get("temporary_bytes")
        memory_bytes = limits.get("memory_bytes")
        output_bytes = limits.get("output_bytes")
        base_image_storage_bytes = plan_payload.get("base_image_storage_bytes")
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
        if snapshot.disk_free_bytes - outstanding_disk_reservation_bytes(
            session, plan.builder_node_id, inventory_observed_at=snapshot.observed_at
        ) < disk_bytes + _build_disk_reserve(snapshot.disk_total_bytes):
            raise RecipeBuildError(
                "build.insufficient_disk", "builder disk capacity changed"
            )
        if (
            _available_build_memory(
                session, snapshot, build=build, request_id=request_id, lock=True
            )
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
            if mapping.state != "ready" or mapping.generation != generation:
                raise RecipeBuildError(
                    "build.mapping_mismatch",
                    "mapping generation does not match the build",
                )
            revision = session.get(CatalogDocumentRevision, mapping.recipe_revision_id)
            if revision is None or revision.content_digest is None:
                raise RecipeBuildError(
                    "build.mapping_mismatch", "mapping recipe is unavailable"
                )
            # Snapshot SQL provenance, then consult its managed-storage owner
            # with no transaction open. Select the bound archive, never another
            # successful output for the same executable build inputs.
            session.close()
            receipt = (
                self._prepared_builds(
                    build.build_input_sha256,
                    expected_architecture=_BUILD_RUNTIME_PLATFORM,
                    expected_runtime_interface=_BUILD_RUNTIME_INTERFACE,
                    expected_archive_sha256=build.oci_layout_sha256,
                )
                if self._prepared_builds is not None
                else None
            )
            if (
                not isinstance(receipt, RuntimeImageReceipt)
                or receipt.build_id != build.id
                or receipt.image_digest != build.image_digest
                or receipt.oci_archive_sha256 != build.oci_layout_sha256
                or receipt.image_bytes != build.image_bytes
            ):
                raise RecipeBuildError(
                    "build.result_unavailable",
                    "exact prepared build archive is unavailable",
                )
            try:
                require_runtime_image_authorization(
                    session,
                    recipe_revision_id=revision.id,
                    current_content_digest=revision.content_digest,
                    receipt=receipt,
                )
            except ValueError as error:
                raise RecipeBuildError("build.mapping_mismatch", str(error)) from error
            current_mapping = session.get(ClusterMapping, mapping_id)
            if (
                current_mapping is None
                or current_mapping.state != "ready"
                or current_mapping.generation != generation
                or current_mapping.recipe_revision_id != revision.id
            ):
                raise RecipeBuildError(
                    "build.mapping_mismatch",
                    "mapping changed during archive verification",
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
    except RecipeRuntimeSpecError:
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


def _available_build_memory(
    session: Session,
    snapshot: InventorySnapshotView,
    *,
    build: RecipeBuild | None = None,
    request_id: str | None = None,
    lock: bool = False,
) -> int:
    inherited = (
        profile_build_memory_claims(
            session,
            build,
            memory_pool=snapshot.memory_pool,
            request_id=request_id,
            lock=lock,
        )
        if build is not None and request_id is not None
        else ()
    )
    if lock:
        session.scalars(
            select(ResourceReservation)
            .where(
                ResourceReservation.node_id == snapshot.node_id,
                ResourceReservation.state.in_(("active", "promised")),
            )
            .order_by(ResourceReservation.id)
            .with_for_update(nowait=True)
        ).all()
    floor = memory_reserve_floor(
        session, snapshot.node_id, memory_pool=snapshot.memory_pool, kind="host-memory"
    )
    capacity = memory_capacity_snapshot(
        snapshot.node_id,
        "host",
        host=(snapshot.host_memory_total_bytes, snapshot.host_memory_free_bytes),
        accelerator=(snapshot.gpu_memory_total_bytes, snapshot.gpu_memory_free_bytes),
        reservations=memory_reservations(
            session,
            snapshot.node_id,
            memory_pool=snapshot.memory_pool,
            excluded_profile_claim_ids=tuple(claim.id for claim in inherited),
        ),
        memory_pool=snapshot.memory_pool,
        evidence_state="fresh" if not snapshot.stale else "stale",
        evidence_observed_at=snapshot.observed_at,
    )
    assert capacity.available_bytes is not None
    assert capacity.occupied_bytes is not None
    assert capacity.reserved_bytes is not None
    assert capacity.unmaterialized_bytes is not None
    return (
        min(
            capacity.available_bytes - capacity.reserved_bytes,
            capacity.available_bytes
            - capacity.occupied_bytes
            - capacity.unmaterialized_bytes
            - sum(
                residual.maximum_bytes
                for residual in capacity.unknown_run_residuals
            ),
        )
        - floor
    )


def _valid_succeeded_receipt(build: RecipeBuild) -> bool:
    """Require complete immutable evidence before considering a cache hit."""
    return build.state == "succeeded" and _has_build_receipt(build)


def _has_build_receipt(build: RecipeBuild) -> bool:
    """Recorded result identity may survive a running or failed replacement."""
    return (
        _OCI_DIGEST.fullmatch(build.image_digest or "") is not None
        and _SHA256.fullmatch(build.oci_layout_sha256 or "") is not None
        and isinstance(build.image_bytes, int)
        and not isinstance(build.image_bytes, bool)
        and build.image_bytes > 0
    )


def _reopen_build_attempt(build: RecipeBuild, *, now: datetime) -> None:
    """Return one build row to a clean planned attempt.

    A failed or cancelled row must not be a permanent barrier to the rebuild
    the operator asked for, and a repaired row must not keep stale result
    evidence.  This clears the terminal state and the recorded result while
    leaving the contract documents to the caller.
    """

    build.state = "planned"
    build.image_digest = None
    build.oci_layout_sha256 = None
    build.image_bytes = None
    build.error = None
    build.updated_at = now


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
