"""Durable source-build planning and exact OCI result recording."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
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
    PrebuiltImage,
    RecipeRevisionProjection,
)
from .categorized_errors import InvalidType, MissingRecord
from .categorized_faults import security_reason
from .content_identity import reusable_build
from .disk_reservations import outstanding_disk_reservation_bytes
from .inventory_repository import InventoryRepository, InventorySnapshotView
from .lifecycle.evidence import BookkeepingReason, retire_as_unknown
from .memory_reservations import memory_reservations, memory_reserve_floor
from .models import (
    AgentNode,
    CatalogDocumentRevision,
    RecipeBuild,
    RecipeSourceBundle,
    ResourceReservation,
)
from .prebuilt_images import (
    PREBUILT_KEY_MISMATCH,
    PREBUILT_NOT_PINNED,
    PREBUILT_RECENT_PULL_FAILURE,
    PREBUILT_USED,
    PrebuiltDecision,
    executable_build_key,
    prebuilt_failed,
)
from .profile_capacity import profile_build_memory_claims
from .recipe_build_receipts import (
    BuildCandidate,
    CompletedRecipeBuild,
    PreparedBuildLookup,
    PreparedBuildReceipt,
)
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
from .source_bundles import (
    GeneratedSourceBundle,
    SourceBundleError,
    SourceBundleRefused,
    SourceBundleStoreProtocol,
    SourceBundleUnknown,
    generate_source_bundle,
)
from .source_policy import (
    SourcePolicyError,
    SourcePolicyReport,
    dockerfile_base_images,
    enforce_build_source_policy,
    inspect_build_source_policy,
)
from .storage_demands import StorageDemands, spark_scope
from .stored_json import read_row_column

_LOGGER = logging.getLogger(__name__)
_OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
BUILD_ARTIFACT_FORMAT = "docker-archive-v1"
#: Bundle read problems that stored data damage does not explain: an invalid
#: request digest, or a store that is unreachable (retry, never re-derive).
_UNHEALABLE_BUNDLE_CODES = frozenset(
    {SourceBundleCode.DIGEST_INVALID, SourceBundleCode.STORAGE_UNAVAILABLE}
)
MINIMUM_BUILD_DISK_RESERVE_BYTES = 4 * 1024**3
MAXIMUM_BUILD_DISK_RESERVE_BYTES = 64 * 1024**3
BUILD_INPUT_IDENTITY_SCHEMA_VERSION = 2
# Controller source builds are always linux/arm64 under the v1 runtime
# contract, so the filesystem build lookup is scoped by the same identity.
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
    """Resolve the platform adaptation for a recipe or fail closed."""
    try:
        return resolve_runtime_adapter(
            projected.runtime_engine, projected.topology.model_dump(mode="json")
        )
    except RuntimeAdapterError as error:
        raise RecipeBuildInvalid(
            RecipeBuildCode.ADAPTER_UNAVAILABLE,
            str(error),
            reason=InvalidRequestReason.NOT_FOUND,
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
            raise InvalidType(
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
        raise RecipeBuildInvalid(
            RecipeBuildCode.RESOURCES_INVALID,
            "canonical runtime compiler did not publish a build resource envelope",
        )
    if security is None:
        raise RecipeBuildInvalid(
            RecipeBuildCode.SECURITY_INVALID,
            "canonical runtime compiler did not publish a build security envelope",
        )
    if any(not isinstance(item, str) or not item for item in security.capabilities):
        raise RecipeBuildInvalid(
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
        raise RecipeBuildInvalid(
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

    def build_input_for_builder(self, binary_digest: str) -> str:
        """Bind canonical executable intent to an accepted builder identity."""
        if _SHA256.fullmatch(binary_digest) is None:
            raise RecipeBuildUnknown(
                RecipeBuildCode.PLAN_INVALID,
                "recorded builder identity is invalid",
                reason=WaitReason.STALE_PLAN,
            )
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


class RecipeBuildService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        bundles: SourceBundleStoreProtocol,
        inventory_max_age: int = 300,
        build_archive_available: Callable[[str, int], bool] | None = None,
        prepared_builds: PreparedBuildLookup | None = None,
        source_rederiver: SourceBundleRederiver = rederive_source_bundle_from_closure,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sessions = sessions
        self._sleep = sleep
        self._bundles = bundles
        self._source_rederiver = source_rederiver
        self._inventory = InventoryRepository(sessions)
        self._inventory_max_age = inventory_max_age
        self._build_archive_available = build_archive_available
        self._prepared_builds = prepared_builds
        self._storage_demands: StorageDemands | None = None

    def bind_storage_demands(self, demands: StorageDemands) -> None:
        """Attach the register a build refused for lack of disk asks space in."""

        self._storage_demands = demands

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

    def _verified_bundle(
        self,
        projected: RecipeRevisionProjection,
        build: Mapping[str, object],
        source_sha256: str,
    ) -> GeneratedSourceBundle:
        """The stored source bundle, healed from evidence when the stored copy is damaged.

        Source is verified at ingress; what the store holds afterwards can be
        damaged (a lost or corrupted copy, metadata that no longer matches) and
        a damaged stored copy is not a recipe fault. A bundle that cannot be
        read, or that lacks the build's Dockerfile, is derived again from the
        recipe package's closure and stored through the same ingress check (the
        digest must match), then read again. Only what is still invalid after
        that fresh verification is the recipe's own fault; when it cannot be
        derived at all the build waits (a library sync restores it) instead of
        being refused.
        """

        dockerfile = build.get("dockerfile")
        context = build.get("context")
        context_path = context.get("path") if isinstance(context, Mapping) else None

        def read() -> GeneratedSourceBundle | SourceBundleError:
            try:
                return self._bundles.get(source_sha256)
            except SourceBundleUnknown:
                raise
            except SourceBundleRefused as error:
                raise RecipeBuildRefused(
                    error.code, str(error), reason=error.typed_reason
                ) from error
            except SourceBundleError as error:
                return error

        def damaged(value: GeneratedSourceBundle | SourceBundleError) -> bool:
            if isinstance(value, SourceBundleError):
                return value.code not in _UNHEALABLE_BUNDLE_CODES
            return not isinstance(dockerfile, str) or dockerfile not in value.files

        loaded = read()
        if damaged(loaded):
            healed = self._heal_source_bundle(projected, context_path, source_sha256)
            if healed:
                loaded = read()
            elif isinstance(loaded, SourceBundleError):
                raise RecipeBuildUnknown(loaded.code, str(loaded)) from loaded
            else:
                raise RecipeBuildUnknown(
                    RecipeBuildCode.SOURCE_UNAVAILABLE,
                    "stored source bundle lacks the recipe Dockerfile and cannot "
                    "be derived again yet",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
        if isinstance(loaded, SourceBundleError):
            raise RecipeBuildUnknown(loaded.code, str(loaded)) from loaded
        return loaded

    def _heal_source_bundle(
        self,
        projected: RecipeRevisionProjection,
        context_path: object,
        source_sha256: str,
    ) -> bool:
        if not isinstance(context_path, str):
            return False
        archive = self._source_rederiver(projected, context_path, source_sha256)
        if archive is None:
            return False
        try:
            self._bundles.put(source_sha256, io.BytesIO(archive))
        except SourceBundleUnknown:
            raise
        except SourceBundleRefused as error:
            raise RecipeBuildRefused(
                error.code, str(error), reason=error.typed_reason
            ) from error
        except SourceBundleError:
            return False
        _LOGGER.warning(
            "source bundle %s was damaged and was derived again", source_sha256
        )
        return True

    def check_source(self, recipe_revision_id: str) -> SourcePolicyReport:
        """Re-observe uncertain build facts after releasing each reading session.
        Unknown outcomes are re-observed with a fixed attempt budget and bounded
        backoff and exact inputs. Security/input refusals escape immediately;
        exhaustion preserves the typed cause and releases resources.
        """
        last_error: UnknownOutcomeError | None = None
        for delay in _BUILD_OBSERVATION_DELAYS:
            if delay:
                self._sleep(delay)
            try:
                return self._check_source_once(recipe_revision_id)
            except UnknownOutcomeError as error:
                last_error = error
        assert last_error is not None
        raise last_error

    def _check_source_once(self, recipe_revision_id: str) -> SourcePolicyReport:
        with self._sessions() as session:
            revision = session.get(CatalogDocumentRevision, recipe_revision_id)
            if revision is None:
                raise MissingRecord(recipe_revision_id)
            if revision.kind != "recipe" or revision.state != "active":
                raise RecipeBuildInvalid(
                    RecipeBuildCode.RECIPE_UNRESOLVED,
                    "only a resolved recipe can be checked",
                )
            projected = _read_recipe_projection(revision)
            document = _canonical_recipe_document(revision.document)
            build = _canonical_build(document, projected)
            source_sha256 = _source_bundle_handle(projected)
            if session.get(RecipeSourceBundle, source_sha256) is None:
                raise RecipeBuildUnknown(
                    RecipeBuildCode.SOURCE_UNAVAILABLE,
                    "verified source bundle is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
        bundle = self._verified_bundle(projected, build, source_sha256)
        return inspect_build_source_policy(
            _source_policy_document(document, build, source_sha256), bundle
        )

    def resolve(self, recipe_revision_id: str) -> RecipeBuildResolution:
        """Re-observe with the bounded, resource-free policy in ``check_source``."""
        last_error: UnknownOutcomeError | None = None
        for delay in _BUILD_OBSERVATION_DELAYS:
            if delay:
                self._sleep(delay)
            try:
                return self._resolve_once(recipe_revision_id)
            except UnknownOutcomeError as error:
                last_error = error
        assert last_error is not None
        raise last_error

    def _resolve_once(self, recipe_revision_id: str) -> RecipeBuildResolution:
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
                raise MissingRecord(recipe_revision_id)
            if revision.kind != "recipe" or revision.state != "active":
                raise RecipeBuildInvalid(
                    RecipeBuildCode.RECIPE_UNRESOLVED,
                    "only a resolved recipe can be built",
                )
            projected = _read_recipe_projection(revision)
            document = _canonical_recipe_document(revision.document)
            build = _canonical_build(document, projected)
            adapter = _resolved_adapter(projected)
            source_sha256 = _source_bundle_handle(projected)
            if session.get(RecipeSourceBundle, source_sha256) is None:
                raise RecipeBuildUnknown(
                    RecipeBuildCode.SOURCE_UNAVAILABLE,
                    "verified source bundle is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )

        bundle = self._verified_bundle(projected, build, source_sha256)
        try:
            enforce_build_source_policy(
                _source_policy_document(document, build, source_sha256), bundle
            )
        except SourcePolicyError as error:
            raise RecipeSourcePolicyError(error.report) from error

        dockerfile_path = build.get("dockerfile")
        dockerfile_payload = (
            bundle.files.get(dockerfile_path)
            if isinstance(dockerfile_path, str)
            else None
        )
        if dockerfile_payload is None:
            raise RecipeBuildInvalid(
                RecipeBuildCode.SOURCE_INVALID,
                "recipe Dockerfile authority is unavailable",
            )
        base_images = list(dockerfile_base_images(dockerfile_payload))
        _canonical_build_resources(projected)
        _declared_image_bytes(document)
        model_inputs = projected.build_model_artifacts
        model_artifacts = (
            model_inputs
            if isinstance(model_inputs, Sequence)
            and not isinstance(model_inputs, (str, bytes))
            else None
        )
        intent = derive_build_input_identity(
            build,
            source_bundle_sha256=source_sha256,
            builder_binary_digest=None,
            artifact_format=BUILD_ARTIFACT_FORMAT,
            base_images=base_images,
            effective_settings=document["settings"],
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
        candidates: list[BuildCandidate] = []
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
                if not reusable_build(
                    resolution,
                    build_input_sha256=candidate.build_input_sha256,
                    source_bundle_sha256=source_sha256,
                    recorded_builder_binary_digest=builder_digest,
                ):
                    continue
                assert candidate.image_digest is not None
                assert candidate.oci_layout_sha256 is not None
                assert candidate.image_bytes is not None
                candidates.append(
                    BuildCandidate(
                        build_id=candidate.id,
                        builder_node_id=candidate.builder_node_id,
                        build_input_sha256=candidate.build_input_sha256,
                        builder_binary_digest=builder_digest,
                        image_digest=candidate.image_digest,
                        oci_layout_sha256=candidate.oci_layout_sha256,
                        image_bytes=candidate.image_bytes,
                    )
                )

        cached: BuildCandidate | None = None
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
            # Incomplete receipt evidence is no cache: it is retired as unknown and
            # the resolution reports a stale receipt, so a fresh build replaces it
            # instead of the damaged record blocking the revision.
            retire_as_unknown(
                "recipe-build.receipt",
                str(cached.build_id),
                BookkeepingReason.ROW_INCOMPLETE,
                "cached source build receipt is incomplete",
            )
            return replace(resolution, stale_receipt=True)
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

    def _admit_spark_build(
        self,
        builder_node_id: str,
        *,
        now: datetime,
        public_network: bool,
        memory_bytes: int,
        disk_envelope: int,
    ) -> None:
        """Admit a Spark build against fresh inventory, disk and memory."""
        try:
            snapshot = self._inventory.latest(
                builder_node_id, now=now, maximum_age=self._inventory_max_age
            )
        except KeyError as error:
            raise RecipeBuildUnknown(
                RecipeBuildCode.INVENTORY_MISSING,
                "fresh builder inventory is unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error
        if snapshot.stale:
            raise RecipeBuildUnknown(
                RecipeBuildCode.INVENTORY_STALE,
                "builder inventory is stale",
                reason=WaitReason.STALE_PLAN,
            )
        if "recipe.build.v1" not in snapshot.capabilities:
            raise RecipeBuildUnknown(
                RecipeBuildCode.CAPABILITY_MISSING,
                "builder does not support typed recipe builds",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        if (
            public_network
            and "recipe.build.egress-proxy.v1" not in snapshot.capabilities
        ):
            raise RecipeBuildUnknown(
                RecipeBuildCode.NETWORK_CAPABILITY_MISSING,
                "fresh builder inventory does not prove the hostname-aware build egress boundary",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        with self._sessions() as session:
            disk_reserved = outstanding_disk_reservation_bytes(
                session, builder_node_id, inventory_observed_at=snapshot.observed_at
            )
            memory_available = _available_build_memory(session, snapshot)
        # Preserve a separate host reserve so an admitted build cannot crowd
        # out the Spark itself.
        disk_needed = disk_envelope + _build_disk_reserve(snapshot.disk_total_bytes)
        if snapshot.disk_free_bytes - disk_reserved < disk_needed:
            if self._storage_demands is not None:
                self._storage_demands.request(
                    spark_scope(builder_node_id),
                    disk_reserved + disk_needed,
                    source="build",
                    subject=builder_node_id,
                    reason=RecipeBuildCode.INSUFFICIENT_DISK,
                )
            raise RecipeBuildUnknown(
                RecipeBuildCode.INSUFFICIENT_DISK,
                "builder lacks temporary disk capacity",
            )
        if memory_available < memory_bytes:
            raise RecipeBuildUnknown(
                RecipeBuildCode.INSUFFICIENT_MEMORY,
                "builder lacks build memory capacity",
            )

    def _usable_prebuilt(
        self,
        recipe_revision_id: str,
        projected: RecipeRevisionProjection,
        *,
        build: Mapping[str, object],
        source_sha256: str,
        base_images: Sequence[Mapping[str, object]],
        adapter: RuntimeAdapter,
        now: datetime,
    ) -> tuple[PrebuiltImage | None, PrebuiltDecision]:
        """The catalog's prebuilt image when it was built from these exact inputs.

        Otherwise ``None``. Either way the decision names why: the image is
        used, the catalog pins none, it was built from other inputs (for
        example under a different platform adapter), or a pull failed
        recently. The decision is stored with the build and logged, so a
        Spark build is never planned without saying why.
        """
        image = projected.prebuilt_image
        if image is None:
            decision = PrebuiltDecision(
                PREBUILT_NOT_PINNED,
                "the signed catalog pins no prebuilt image for this revision",
            )
            _LOGGER.info("recipe revision %s: %s", recipe_revision_id, decision)
            return None, decision
        key = executable_build_key(
            derive_build_input_identity(
                build,
                source_bundle_sha256=source_sha256,
                builder_binary_digest=None,
                base_images=base_images,
                runtime_adapter=adapter.document(),
            )
        )
        if key != image.build_key:
            decision = PrebuiltDecision(
                PREBUILT_KEY_MISMATCH,
                f"prebuilt image {image.reference} was built from other inputs "
                f"(catalog key {image.build_key}, Controller key {key})",
            )
            _LOGGER.warning(
                "recipe revision %s: %s; building on a Spark instead",
                recipe_revision_id,
                decision,
            )
            return None, decision
        with self._sessions() as session:
            failed = prebuilt_failed(session, recipe_revision_id, image, now=now)
        if failed is not None:
            decision = PrebuiltDecision(PREBUILT_RECENT_PULL_FAILURE, failed)
            _LOGGER.warning(
                "recipe revision %s: %s; building on a Spark instead",
                recipe_revision_id,
                decision,
            )
            return None, decision
        decision = PrebuiltDecision(
            PREBUILT_USED, f"pulling the catalog's prebuilt image {image.reference}"
        )
        _LOGGER.info("recipe revision %s: %s", recipe_revision_id, decision)
        return image, decision

    def prepare_plan(
        self,
        recipe_revision_id: str,
        builder_node_id: str,
        *,
        now: datetime,
        resolution: RecipeBuildResolution | None = None,
    ) -> RecipeBuildPlan:
        """Re-observe with the bounded, resource-free policy in ``check_source``."""
        last_error: UnknownOutcomeError | None = None
        for delay in _BUILD_OBSERVATION_DELAYS:
            if delay:
                self._sleep(delay)
            try:
                return self._prepare_plan_once(
                    recipe_revision_id, builder_node_id, now=now, resolution=resolution
                )
            except UnknownOutcomeError as error:
                last_error = error
        assert last_error is not None
        raise last_error

    def _prepare_plan_once(
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
                raise MissingRecord(recipe_revision_id)
            if revision.kind != "recipe" or revision.state != "active":
                raise RecipeBuildInvalid(
                    RecipeBuildCode.RECIPE_UNRESOLVED,
                    "only a resolved recipe can be built",
                )
            projected = _read_recipe_projection(revision)
            node = session.get(AgentNode, builder_node_id)
            if node is None:
                raise RecipeBuildUnknown(
                    RecipeBuildCode.NODE_UNKNOWN,
                    "builder GPU node is unknown",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
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
                raise RecipeBuildUnknown(
                    RecipeBuildCode.SOURCE_UNAVAILABLE,
                    "verified source bundle is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
        bundle = self._verified_bundle(projected, build, source_sha256)
        try:
            policy = enforce_build_source_policy(
                _source_policy_document(document, build, source_sha256), bundle
            )
        except SourcePolicyError as error:
            raise RecipeSourcePolicyError(error.report) from error
        dockerfile_path = build.get("dockerfile") if isinstance(build, dict) else None
        dockerfile_payload = (
            bundle.files.get(dockerfile_path)
            if isinstance(dockerfile_path, str)
            else None
        )
        if dockerfile_payload is None:
            raise RecipeBuildInvalid(
                RecipeBuildCode.SOURCE_INVALID,
                "recipe Dockerfile authority is unavailable",
            )
        base_images = list(dockerfile_base_images(dockerfile_payload))
        prebuilt, prebuilt_decision = self._usable_prebuilt(
            revision.id,
            projected,
            build=build,
            source_sha256=source_sha256,
            base_images=base_images,
            adapter=adapter,
            now=now,
        )
        if prebuilt is not None:
            # The Controller pulls this image; the Spark builds nothing, so
            # no Spark inventory, disk or memory is admitted for it. The
            # builder stays the nominal owner of the row, and the pinned
            # manifest digest stands in for the builder binary identity.
            builder_binary_digest = prebuilt.digest.removeprefix("sha256:")
        resources, security = _canonical_build_resources(projected)
        temporary_bytes = resources.temporary_bytes
        memory_bytes = resources.memory_bytes
        cpu_cores = resources.cpu_cores
        processes = resources.processes
        capabilities = list(security.capabilities)
        output_bytes = _declared_image_bytes(document)
        base_image_storage_bytes = resources.download_bytes if base_images else 0
        if prebuilt is None:
            try:
                self._admit_spark_build(
                    builder_node_id,
                    now=now,
                    public_network=public_network,
                    memory_bytes=memory_bytes,
                    # The rootless builder retains inputs while exporting the
                    # image. Treat recipe storage as a generous peak envelope,
                    # not an exact quota over Podman's implementation-specific
                    # graph.
                    disk_envelope=_build_disk_envelope(
                        base_image_bytes=base_image_storage_bytes,
                        temporary_bytes=temporary_bytes,
                        source_bytes=len(bundle.archive),
                        output_bytes=output_bytes,
                    ),
                )
            except RecipeBuildError as error:
                error.prebuilt_unused = (
                    None if prebuilt_decision.used else prebuilt_decision
                )
                raise
        model_inputs = projected.build_model_artifacts
        build_identity = derive_build_input_identity(
            build,
            source_bundle_sha256=source_sha256,
            builder_binary_digest=builder_binary_digest,
            artifact_format=BUILD_ARTIFACT_FORMAT,
            base_images=base_images,
            effective_settings=document["settings"],
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
                raise RecipeBuildRefused(
                    RecipeBuildCode.RESOLUTION_STALE,
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
        }
        payload: dict[str, object] = {
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
            "network": copy.deepcopy(build["network"]),
            "options": (
                projected.build_options.model_dump(mode="json")
                if projected.build_options is not None
                else {}
            ),
            "limits": limits,
        }
        policy_document = {
            "passed": policy.passed,
            "source_bundle_sha256": policy.source_bundle_sha256,
            "dockerfile": policy.dockerfile,
            "findings": [asdict(item) for item in policy.findings],
            "builder_binary_digest": builder_binary_digest,
            "artifact_format": BUILD_ARTIFACT_FORMAT,
        }
        policy_document["prebuilt_decision"] = {
            "code": prebuilt_decision.code,
            "detail": prebuilt_decision.detail,
        }
        if prebuilt is not None:
            policy_document["prebuilt_image"] = prebuilt.reference
        try:
            # Persist the canonical JSON-mode representation.  This is also
            # the representation handed to the agent build queue.
            payload = build_plan_document(payload)
            policy_document = build_policy_document(policy_document)
        except RecipeExecutionContractError as error:
            raise RecipeBuildUnknown(
                RecipeBuildCode.CONTRACT_INVALID,
                "source build envelope is invalid",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
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

    def reusable_build_id(self, recipe_revision_id: str) -> str | None:
        """The succeeded build whose inputs this revision reproduces, whoever built it.

        The executable inputs identify an image, not the revision that first
        produced it, nor the builder binary that happened to build it: the
        same resolution that lets a download reuse a build. It needs no
        capacity and creates or changes nothing.
        """

        try:
            return self.resolve(recipe_revision_id).build_id
        except (UnknownOutcomeError, SecurityRefusalError):
            raise
        except (RecipeBuildError, KeyError, TypeError, ValueError):
            return None

    def persist_plan_in_session(
        self, session: Session, plan: RecipeBuildPlan, *, now: datetime
    ) -> RecipeBuildPlan:
        """Persist a prepared plan using the caller's transaction.

        This helper intentionally never opens another transaction.  It may be
        called while the availability parent and builder rows are locked.
        """
        try:
            acquire_admission_keys(
                session,
                (node_admission_key(plan.builder_node_id),),
                holder="recipe-build",
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
            raise RecipeBuildUnknown(
                RecipeBuildCode.NODE_UNKNOWN,
                "builder GPU node is unknown",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        _validate_builder(node)
        policy_document = plan.policy_report
        if not isinstance(policy_document, dict):
            raise RecipeBuildUnknown(
                RecipeBuildCode.PLAN_INVALID,
                "prepared source build policy is unavailable",
                reason=WaitReason.STALE_PLAN,
            )
        try:
            policy = parse_stored_build_policy(policy_document)
        except RecipeExecutionContractError as error:
            raise RecipeBuildUnknown(
                RecipeBuildCode.PLAN_INVALID,
                "prepared source build policy is invalid" + error.detail,
                reason=WaitReason.STALE_PLAN,
            ) from error
        if (
            policy.prebuilt_image is None
            and policy.builder_binary_digest != node.binary_digest
        ):
            raise RecipeBuildUnknown(
                RecipeBuildCode.RUNTIME_CHANGED,
                "builder runtime identity changed",
                reason=WaitReason.SCOPE_CHANGED,
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
                    raise RecipeBuildUnknown(
                        RecipeBuildCode.PLAN_INVALID,
                        "stored source build envelope is invalid" + error.detail,
                        reason=WaitReason.STALE_PLAN,
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
            raise RecipeBuildUnknown(
                RecipeBuildCode.PLAN_INVALID,
                "stored source build plan is invalid" + error.detail,
                reason=WaitReason.STALE_PLAN,
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
            raise RecipeBuildRefused(
                RecipeBuildCode.EVIDENCE_INVALID, "build result evidence is invalid"
            )
        with self._sessions.begin() as session:
            build = session.get(RecipeBuild, build_id, with_for_update=True)
            if build is None:
                raise MissingRecord(build_id)
            if build.build_input_sha256 != build_input_sha256:
                raise RecipeBuildRefused(
                    RecipeBuildCode.INPUT_MISMATCH,
                    "build result does not match its inputs",
                )
            if build.state == "succeeded":
                if (
                    build.image_digest != image_digest
                    or build.oci_layout_sha256 != oci_layout_sha256
                    or build.image_bytes != image_bytes
                ):
                    raise RecipeBuildRefused(
                        RecipeBuildCode.RESULT_CONFLICT,
                        "build already has different evidence",
                    )
            elif build.state not in {"planned", "building"}:
                raise RecipeBuildRefused(
                    RecipeBuildCode.STATE, "failed build cannot accept success evidence"
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
                session,
                (node_admission_key(plan.builder_node_id),),
                holder="recipe-build",
            )
            self._reserve_in_session(session, plan, now=now, request_id=request_id)
        except AdmissionLockBusy as error:
            raise RecipeBuildAdmissionBusy() from error
        except RecipeBuildError:
            raise
        except ValueError as error:
            raise RecipeBuildInvalid(
                RecipeBuildCode.CAPACITY_CONTRACT_INVALID, str(error)
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
                    select(AgentNode).where(AgentNode.node_id == plan.builder_node_id),
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
            raise RecipeBuildRefused(
                RecipeBuildCode.DEPENDENCIES_STALE, "exact recipe dependencies changed"
            )
        if build is None:
            raise RecipeBuildUnknown(
                RecipeBuildCode.PLAN_INVALID,
                "stored build identity is invalid",
                reason=WaitReason.STALE_PLAN,
            )
        try:
            stored_policy = parse_stored_build_policy(build.policy_report)
            parse_stored_build_plan(build.plan)
            requested_plan = parse_stored_build_plan(plan.agent_payload)
        except RecipeExecutionContractError as error:
            raise RecipeBuildUnknown(
                RecipeBuildCode.PLAN_INVALID,
                "stored source build envelope is invalid" + error.detail,
                reason=WaitReason.STALE_PLAN,
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
            raise RecipeBuildUnknown(
                RecipeBuildCode.PLAN_INVALID,
                "stored build identity is invalid",
                reason=WaitReason.STALE_PLAN,
            )
        try:
            snapshot = self._inventory.latest(
                plan.builder_node_id, now=now, maximum_age=self._inventory_max_age
            )
        except KeyError as error:
            raise RecipeBuildUnknown(
                RecipeBuildCode.INVENTORY_MISSING,
                "fresh builder inventory is unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error
        if node is None:
            raise RecipeBuildUnknown(
                RecipeBuildCode.NODE_UNKNOWN,
                "builder GPU node is unknown",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        _validate_builder(node)
        if node.binary_digest != expected_binary_digest:
            raise RecipeBuildUnknown(
                RecipeBuildCode.RUNTIME_CHANGED,
                "builder runtime identity changed",
                reason=WaitReason.SCOPE_CHANGED,
            )
        if snapshot.stale:
            raise RecipeBuildUnknown(
                RecipeBuildCode.INVENTORY_STALE,
                "builder inventory is stale",
                reason=WaitReason.STALE_PLAN,
            )
        plan_payload = build_plan_document(requested_plan)
        limits = plan_payload.get("limits")
        source_bytes = plan_payload.get("source_bundle_bytes")
        if not isinstance(limits, dict) or not isinstance(source_bytes, int):
            raise RecipeBuildUnknown(
                RecipeBuildCode.PLAN_INVALID,
                "build plan is invalid",
                reason=WaitReason.STALE_PLAN,
            )
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
            raise RecipeBuildUnknown(
                RecipeBuildCode.PLAN_INVALID,
                "build plan is invalid",
                reason=WaitReason.STALE_PLAN,
            )
        disk_bytes = _build_disk_envelope(
            base_image_bytes=base_image_storage_bytes,
            temporary_bytes=temporary_bytes,
            source_bytes=source_bytes,
            output_bytes=output_bytes,
        )
        if snapshot.disk_free_bytes - outstanding_disk_reservation_bytes(
            session, plan.builder_node_id, inventory_observed_at=snapshot.observed_at
        ) < disk_bytes + _build_disk_reserve(snapshot.disk_total_bytes):
            raise RecipeBuildUnknown(
                RecipeBuildCode.INSUFFICIENT_DISK, "builder disk capacity changed"
            )
        if (
            _available_build_memory(
                session, snapshot, build=build, request_id=request_id, lock=True
            )
            < memory_bytes
        ):
            raise RecipeBuildUnknown(
                RecipeBuildCode.INSUFFICIENT_MEMORY, "builder memory capacity changed"
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
                    state=ReservationState.ACTIVE,
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
                    state=ReservationState.ACTIVE,
                    plan_digest=plan.build_input_sha256,
                    created_at=now,
                ),
            )
        )


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
        raise RecipeBuildInvalid(
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
