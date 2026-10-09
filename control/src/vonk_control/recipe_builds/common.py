"""Recipe builds: common concerns."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InvalidRequestError,
    InvalidRequestReason,
    RecipeBuildCode,
    ReservationState,
    SecurityRefusalError,
    SecurityRefusalReason,
    SourceBundleCode,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol.build_import import RecipeBuildOptions
from vonk_forge_contracts import read_recipe
from vonk_forge_contracts.recipe import RecipeSetting, RecipeSettings

from ..catalog_revision_contract import (
    BuildModelArtifactProjection,
    BuildResourcesProjection,
    BuildSecurityProjection,
    RecipeRevisionProjection,
)
from ..categorized_errors import InvalidType
from ..categorized_faults import security_reason
from ..inventory_repository import InventorySnapshotView
from ..memory_reservations import memory_reservations, memory_reserve_floor
from ..models import (
    AgentNode,
    CatalogDocumentRevision,
    RecipeBuild,
    ResourceReservation,
)
from ..prebuilt_images import PrebuiltDecision
from ..profile_capacity import profile_build_memory_claims
from ..recipe_runtime_specs import RecipeRuntimeSpecError, recipe_topology
from ..resource_planning import memory_capacity_snapshot
from ..runtime_adapters import (
    RuntimeAdapter,
    RuntimeAdapterError,
    resolve_runtime_adapter,
)
from ..source_bundles import (
    GeneratedSourceBundle,
    SourceBundleError,
    generate_source_bundle,
)
from ..source_policy import SourcePolicyReport, inspect_build_source_policy
from ..stored_json import read_row_column

"""Durable source-build planning and exact OCI result recording."""


_LOGGER = logging.getLogger("vonk_control.recipe_builds")


_OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


BUILD_ARTIFACT_FORMAT = "docker-archive-v1"


_UNHEALABLE_BUNDLE_CODES = frozenset(
    {SourceBundleCode.DIGEST_INVALID, SourceBundleCode.STORAGE_UNAVAILABLE}
)


MINIMUM_BUILD_DISK_RESERVE_BYTES = 4 * 1024**3


MAXIMUM_BUILD_DISK_RESERVE_BYTES = 64 * 1024**3


BUILD_INPUT_IDENTITY_SCHEMA_VERSION = 2


_BUILD_RUNTIME_PLATFORM = "linux/arm64"


_BUILD_RUNTIME_INTERFACE = "vonk.runtime.v1"


_RECIPE_SETTINGS = TypeAdapter(RecipeSettings)


_BUILD_OBSERVATION_DELAYS = (0.0, 0.1, 0.2)


def derive_build_input_identity(
    build: Mapping[str, object],
    *,
    source_bundle_sha256: str,
    builder_binary_digest: str | None,
    artifact_format: str = BUILD_ARTIFACT_FORMAT,
    base_images: Sequence[Mapping[str, object]] = (),
    effective_settings: object | None = None,
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
    if model_artifacts is not None:
        identity["model_artifacts"] = _canonical_model_build_inputs(model_artifacts)
    if runtime_adapter is not None:
        identity["runtime_adapter"] = copy.deepcopy(dict(runtime_adapter))
    return identity


def _resolved_adapter(projected: RecipeRevisionProjection) -> RuntimeAdapter:
    """Observe the accepted compiler adaptation without another refusal gate."""
    try:
        return resolve_runtime_adapter(
            projected.runtime_engine, projected.topology.model_dump(mode="json")
        )
    except RuntimeAdapterError as error:
        raise RecipeBuildUnknown(
            RecipeBuildCode.ADAPTER_UNAVAILABLE,
            str(error),
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error


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
        if not isinstance(value, Mapping):
            raise InvalidType("stored recipe is not a JSON object")
        read_recipe(value)
    except (TypeError, ValueError) as error:
        raise RecipeBuildUnknown(
            RecipeBuildCode.CONTRACT_INVALID,
            "stored recipe does not satisfy RecipeDefinition",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error
    return dict(value)


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
            raise RecipeBuildUnknown(
                RecipeBuildCode.CONTRACT_INVALID,
                "model build input projection is unavailable",
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


def canonical_build(
    document: Mapping[str, object],
    *,
    options: RecipeBuildOptions | None = None,
    security: BuildSecurityProjection | None = None,
    compile_policy: bool = True,
) -> Mapping[str, object]:
    """Project ``execution.build`` plus the platform build policy it runs under.

    Executable platform policy participates in the same cache identity as the
    Dockerfile and base images. Resource quotas do not.
    """
    execution = document.get("execution")
    build = execution.get("build") if isinstance(execution, Mapping) else None
    if not isinstance(build, Mapping):
        raise RecipeBuildUnknown(
            RecipeBuildCode.CONTRACT_INVALID,
            "canonical execution.build is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    compiled = {**build, "dockerfile": _bundle_dockerfile_path(build)}
    if compile_policy:
        compiled["options"] = (
            options.model_dump(mode="json") if options is not None else {}
        )
        compiled["security"] = (
            security.model_dump(mode="json") if security is not None else {}
        )
    return compiled


def _canonical_build(
    document: Mapping[str, object], projected: RecipeRevisionProjection | None = None
) -> Mapping[str, object]:
    if projected is None:
        return canonical_build(document, compile_policy=False)
    return canonical_build(
        document, options=projected.build_options, security=projected.build_security
    )


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
        raise RecipeBuildUnknown(
            RecipeBuildCode.SOURCE_UNAVAILABLE,
            "catalog package handle is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    return candidate


def _canonical_build_resources(
    projected: RecipeRevisionProjection,
) -> tuple[BuildResourcesProjection, BuildSecurityProjection]:
    """Read compiler-owned build admission projections from the catalog row."""
    resources = projected.build_resources
    security = projected.build_security
    if resources is None:
        raise RecipeBuildUnknown(
            RecipeBuildCode.RESOURCES_INVALID,
            "canonical runtime compiler did not publish a build resource envelope",
        )
    if security is None:
        raise RecipeBuildUnknown(
            RecipeBuildCode.SECURITY_INVALID,
            "canonical runtime compiler did not publish a build security envelope",
        )
    if any(not isinstance(item, str) or not item for item in security.capabilities):
        raise RecipeBuildUnknown(
            RecipeBuildCode.SECURITY_INVALID, "canonical build capabilities are invalid"
        )
    return resources, security


def _source_policy_document(
    document: Mapping[str, object],
    build: Mapping[str, object],
    source_sha256: str,
) -> dict[str, object]:
    """Adapt canonical execution.build to the source-policy parser boundary."""
    context = build.get("context")
    context_path = context.get("path") if isinstance(context, Mapping) else None
    if not isinstance(context_path, str):
        raise RecipeBuildUnknown(
            RecipeBuildCode.SOURCE_INVALID, "canonical build context path is invalid"
        )
    normalized_build = {
        "context": {"path": context_path, "sha256": source_sha256},
        "dockerfile": build.get("dockerfile"),
        "network": copy.deepcopy(build.get("network", {"hosts": []})),
    }
    return {**copy.deepcopy(dict(document)), "build": normalized_build}


def inspect_package_source_policy(
    document: Mapping[str, object],
    bundle: GeneratedSourceBundle,
    *,
    source_sha256: str | None = None,
) -> SourcePolicyReport:
    """Apply the Controller's build source policy to a recipe and its context.

    ``prepare_plan`` runs the same :func:`inspect_build_source_policy` over the
    same :func:`_source_policy_document`; the recipe library validation and the
    prebuilt-image planner call this instead of a copy, so a recipe the
    Controller would refuse can neither pass validation nor publish an image.
    ``source_sha256`` is the digest the catalog binds to the recipe, when it
    differs from the bundle's own.
    """

    build = canonical_build(_canonical_recipe_document(document), compile_policy=False)
    return inspect_build_source_policy(
        _source_policy_document(document, build, source_sha256 or bundle.sha256),
        bundle,
    )


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
        # Why the catalog's prebuilt image was not used, when a Spark build
        # was planned instead and could not be admitted.
        self.prebuilt_unused: PrebuiltDecision | None = None
        super().__init__(detail)


class RecipeBuildRefused(SecurityRefusalError, RecipeBuildError):
    """A refusal at a security boundary: build evidence or dependencies that do not match the authorized build."""

    def __init__(
        self,
        *args: Any,
        reason: SecurityRefusalReason | None = None,
        **fields: Any,
    ) -> None:
        RecipeBuildError.__init__(self, *args, **fields)
        self.typed_reason = (
            reason if reason is not None else security_reason(args[0] if args else None)
        )


class RecipeBuildInvalid(InvalidRequestError, RecipeBuildError):
    """A malformed or out-of-contract build request, source or resource declaration."""

    def __init__(
        self,
        *args: Any,
        reason: InvalidRequestReason | None = InvalidRequestReason.MALFORMED,
        field: str | None = None,
        **fields: Any,
    ) -> None:
        RecipeBuildError.__init__(self, *args, **fields)
        self.typed_reason = reason
        self.typed_field = field


class RecipeBuildUnknown(UnknownOutcomeError, RecipeBuildError):
    """Missing or stale inventory, capacity or bookkeeping: the owner observes it again; never a refusal."""

    def __init__(
        self,
        *args: Any,
        reason: WaitReason | None = None,
        **fields: Any,
    ) -> None:
        RecipeBuildError.__init__(self, *args, **fields)
        self.typed_reason = reason


class RecipeSourcePolicyError(InvalidRequestError, RecipeBuildError):
    """The recipe's stored build source violates the Controller's source policy.

    Retrying cannot change the answer: only a different source bundle can.  The
    report keeps every finding with its file and line so the refusal can name
    them instead of the first finding's bare sentence.
    """

    def __init__(self, report: SourcePolicyReport) -> None:
        finding = report.findings[0]
        super().__init__(finding.code, report.describe())
        self.report = report


class RecipeBuildAdmissionBusy(UnknownOutcomeError, RecipeBuildError):
    code = RecipeBuildCode.CAPACITY_BUSY

    def __init__(self) -> None:
        super().__init__(self.code, "builder capacity writer is busy")


def _read_recipe_projection(
    revision: CatalogDocumentRevision,
) -> RecipeRevisionProjection:
    projected = read_row_column(revision, "projected")
    if not isinstance(projected, RecipeRevisionProjection):
        raise RecipeBuildUnknown(
            RecipeBuildCode.CONTRACT_INVALID,
            "stored recipe catalog projection is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
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

    def build_input_for_builder(self, binary_digest: str) -> str | None:
        """Bind canonical executable intent to an accepted builder identity."""
        if _SHA256.fullmatch(binary_digest) is None:
            # A damaged cached receipt is not a reusable content identity.
            # No effect was accepted: resolution can examine the next receipt
            # or prepare a fresh build without retaining a claim.
            return None
        return _digest(self.input_intent | {"builder_binary_digest": binary_digest})


class SourceBundleRederiver(Protocol):
    """Produce a source bundle's archive again from the evidence the Controller kept.

    The answer is only a candidate: the bundle store verifies it against the
    digest at ingress, so a wrong or damaged derivation can never be trusted.
    """

    def __call__(
        self,
        projected: RecipeRevisionProjection,
        context_path: str,
        source_sha256: str,
        /,
    ) -> bytes | None: ...


def rederive_source_bundle_from_closure(
    projected: RecipeRevisionProjection, context_path: str, source_sha256: str
) -> bytes | None:
    """Rebuild the build-context bundle from the recipe package's stored closure.

    The closure is the verified package contents the catalog import recorded
    beside its digest-addressed package; the bundle is exactly the files under
    the build context, generated the way the import generated it.
    """

    handle = projected.package_handle
    if handle is None:
        return None
    closure = Path(handle.closure_path)
    root = closure / context_path.strip("/")
    try:
        if (closure / ".complete").read_text(encoding="ascii") != handle.package_sha256:
            return None
        files = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }
        bundle = generate_source_bundle(files)
    except PermissionError as error:
        raise RecipeBuildRefused(
            SecurityRefusalReason.PERMISSION_DENIED.value,
            "recipe source closure access was denied",
            reason=SecurityRefusalReason.PERMISSION_DENIED,
        ) from error
    except (OSError, ValueError, SourceBundleError):
        return None
    return bundle.archive if bundle.sha256 == source_sha256 else None


def _validate_builder(node: AgentNode) -> None:
    if (
        node.state != "active"
        or node.revoked_at is not None
        or node.architecture != "linux-arm64"
        or not isinstance(node.binary_digest, str)
        or _SHA256.fullmatch(node.binary_digest) is None
    ):
        raise RecipeBuildUnknown(
            RecipeBuildCode.NODE_INCOMPATIBLE,
            "builder GPU node is inactive or incompatible",
        )


def _public_build_network(build: object) -> bool:
    if not isinstance(build, dict):
        return False
    network = build.get("network")
    return isinstance(network, dict) and bool(network.get("hosts"))


def _declared_image_bytes(document: dict[str, object]) -> int:
    try:
        values = [
            role.resources.disk.image_bytes for role in recipe_topology(document).roles
        ]
    except RecipeRuntimeSpecError:
        values = []
    if not values or min(values) < 1 or max(values) > 16 * 1024**4:
        raise RecipeBuildUnknown(
            RecipeBuildCode.IMAGE_SIZE_INVALID,
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
                ResourceReservation.state.in_(
                    (ReservationState.ACTIVE, ReservationState.PROMISED)
                ),
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
                residual.maximum_bytes for residual in capacity.unknown_run_residuals
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
