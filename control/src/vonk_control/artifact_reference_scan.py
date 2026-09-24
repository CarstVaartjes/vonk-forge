"""Fail-closed reads of current model/image reference owners.

These projections are used only while taking a deletion fence. They never
become availability facts and never copy the owners into a second reference
table.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import canonical_message

from .artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    lock_reference_gates,
)
from .fleet_profile_contract import (
    FleetProfileAssignmentInput,
    FleetProfilePreview,
)
from .model_cache_contract import CacheManifest
from .models import (
    ArtifactDistributionAssignment,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeInstallation,
    RecipeRun,
    RuntimeImageAuthorization,
)
from .run_switch_contract import (
    RunSwitchOperationResult,
    RunSwitchPlan,
    RunSwitchRuntimeImageReferenceIntent,
)
from .runtime_image_preparation import (
    RuntimeImagePreparationError,
    read_runtime_image_reference_intent,
)

_ACTIVE_PROFILE_APPLICATIONS = ("queued", "running", "waiting-for-operator")
_ACTIVE_RUN_SWITCH_JOBS = ("queued", "running", "partial", "waiting-for-operator")
_ACTIVE_INSTALLATIONS = ("planned", "installing", "installed", "partial")
_ACTIVE_RUNS = ("starting", "running", "stopping")
_ACTIVE_ARTIFACT_JOBS = (
    "queued",
    "running",
    "partial",
    "waiting-for-operator",
    "cancelling",
)
_PROFILE_PAYLOAD_BUDGET = 16 * 1024 * 1024
MAX_ARTIFACT_OWNER_SCAN_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ArtifactReferenceFinding:
    """One validated existing owner of an exact managed artifact identity."""

    asset: ArtifactIdentity
    owner_kind: str
    owner_id: str
    state: str
    classification: Literal["saved-reference", "active-work"]
    detail: str
    reason: str


def _finding(
    kind: Literal["model-set", "model-object", "runtime-image"],
    digest: str,
    *,
    owner_kind: str,
    owner_id: str,
    state: str,
    classification: Literal["saved-reference", "active-work"],
    detail: str,
    reason: str,
) -> ArtifactReferenceFinding:
    return ArtifactReferenceFinding(
        asset=ArtifactIdentity(kind, digest),
        owner_kind=owner_kind,
        owner_id=owner_id,
        state=state,
        classification=classification,
        detail=detail,
        reason=reason,
    )


def _reason_projection(
    findings: Mapping[str, tuple[ArtifactReferenceFinding, ...]],
) -> dict[str, tuple[str, ...]]:
    return {
        digest: tuple(sorted({finding.reason for finding in values}))
        for digest, values in findings.items()
    }


def require_model_sets_open(
    session: Session,
    set_digests: Iterable[str],
    *,
    now: datetime,
    object_digests: Iterable[str] = (),
) -> dict[str, tuple[str, ...]]:
    """Lock exact set gates, validate memberships, then lock every object gate.

    Call this inside the same short transaction that creates or accepts the
    existing request/profile/distribution owner. Model-set gates are acquired
    first everywhere, followed by object gates in digest order.
    """

    requested = tuple(sorted(set(set_digests)))
    if not requested:
        return {}
    set_rows = lock_reference_gates(
        session,
        (ArtifactIdentity("model-set", digest) for digest in requested),
        now=now,
    )
    if any(row.removal_owner_id is not None for row in set_rows):
        raise ArtifactLifecycleError(
            "artifact.deletion_in_progress",
            "a model artifact set is reserved for removal",
            retryable=True,
        )
    existing_sets = tuple(
        session.scalars(
            select(ModelCacheSet.artifact_set_sha256)
            .where(ModelCacheSet.artifact_set_sha256.in_(requested))
            .order_by(ModelCacheSet.artifact_set_sha256)
        )
    )
    objects_by_set = model_set_objects(session, existing_sets)
    expected_objects = tuple(sorted(set(object_digests)))
    if expected_objects and len(requested) != 1:
        raise ValueError(
            "explicit model object identities require exactly one model set"
        )
    if (
        expected_objects
        and existing_sets
        and objects_by_set[requested[0]] != expected_objects
    ):
        raise ArtifactLifecycleError(
            "artifact.reference_identity_mismatch",
            "accepted model object identities disagree with the current model-set membership",
        )
    all_objects = {digest for values in objects_by_set.values() for digest in values}
    all_objects.update(expected_objects)
    object_identities = tuple(
        ArtifactIdentity("model-object", digest) for digest in sorted(all_objects)
    )
    object_rows = lock_reference_gates(session, object_identities, now=now)
    if any(row.removal_owner_id is not None for row in object_rows):
        raise ArtifactLifecycleError(
            "artifact.deletion_in_progress",
            "a model artifact object is reserved for removal",
            retryable=True,
        )
    return objects_by_set


def model_set_objects(
    session: Session, set_digests: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """Return verified SQL membership identities for exact model-set digests.

    The typed manifest and membership rows must agree. A failed or oversized
    scan is a blocker; it is never interpreted as an empty set.
    """

    requested = tuple(sorted(set(set_digests)))
    if not requested:
        return {}
    sets = {
        row.artifact_set_sha256: row
        for row in session.scalars(
            select(ModelCacheSet)
            .where(ModelCacheSet.artifact_set_sha256.in_(requested))
            .order_by(ModelCacheSet.artifact_set_sha256)
        )
    }
    if set(sets) != set(requested):
        raise ArtifactLifecycleError(
            "artifact.reference_scan_failed",
            "exact model-set membership is unavailable; removal was deferred",
            retryable=True,
        )
    memberships: dict[str, list[ModelCacheSetArtifact]] = {
        digest: [] for digest in requested
    }
    for row in session.scalars(
        select(ModelCacheSetArtifact)
        .where(ModelCacheSetArtifact.artifact_set_sha256.in_(requested))
        .order_by(
            ModelCacheSetArtifact.artifact_set_sha256,
            ModelCacheSetArtifact.artifact_key,
        )
    ):
        memberships[row.artifact_set_sha256].append(row)
    result: dict[str, tuple[str, ...]] = {}
    for set_digest in requested:
        row = sets[set_digest]
        try:
            manifest = CacheManifest.model_validate_json(
                canonical_message(row.manifest), strict=True
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise ArtifactLifecycleError(
                "artifact.reference_scan_failed",
                "model-set manifest is malformed; removal was deferred",
            ) from error
        expected = {item.key: (item.sha256, item.path) for item in manifest.artifacts}
        observed = {
            item.artifact_key: (item.artifact_sha256, item.path)
            for item in memberships[set_digest]
        }
        if expected != observed:
            raise ArtifactLifecycleError(
                "artifact.reference_scan_failed",
                "model-set membership disagrees with its manifest; removal was deferred",
            )
        result[set_digest] = tuple(sorted({item[0] for item in observed.values()}))
    return result


def model_set_reference_findings(
    session: Session, set_digests: Iterable[str]
) -> dict[str, tuple[ArtifactReferenceFinding, ...]]:
    """Find typed saved-profile and active owners of exact model sets."""

    selected = set(set_digests)
    if not selected:
        return {}
    sets = {
        row.artifact_set_sha256: row
        for row in session.scalars(
            select(ModelCacheSet)
            .where(ModelCacheSet.artifact_set_sha256.in_(selected))
            .order_by(ModelCacheSet.artifact_set_sha256)
        )
    }
    if set(sets) != selected:
        raise ArtifactLifecycleError(
            "artifact.reference_scan_failed",
            "model-set owners could not be read; removal was deferred",
            retryable=True,
        )
    findings: dict[str, set[ArtifactReferenceFinding]] = {
        digest: set() for digest in selected
    }

    # A saved profile remains a protective selector reference. Resolve it
    # against the current active recipe head and protect every exact cache set
    # for that recipe; model_variant may select a subset, so protecting all
    # current variants is deliberately conservative until the operator edits
    # the profile or reviews a new exact application.
    profile_bytes = 0
    try:
        profile_rows = session.scalars(select(FleetProfile).order_by(FleetProfile.id))
        for profile in profile_rows:
            encoded = canonical_message(profile.assignments)
            profile_bytes += len(encoded)
            if profile_bytes > _PROFILE_PAYLOAD_BUDGET:
                raise ArtifactLifecycleError(
                    "artifact.reference_scan_limited",
                    f"saved-profile reference scan exceeded {_PROFILE_PAYLOAD_BUDGET} bytes; removal was deferred",
                    retryable=True,
                )
            assignments = TypeAdapter(list[FleetProfileAssignmentInput]).validate_json(
                encoded, strict=True
            )
            for assignment in assignments:
                publisher, separator, slug = assignment.recipe_selector.partition("/")
                if not separator:
                    raise ValueError("stored recipe selector is invalid")
                revisions = session.scalars(
                    select(CatalogDocumentRevision)
                    .where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.state == "active",
                        CatalogDocumentRevision.publisher == publisher,
                        CatalogDocumentRevision.slug == slug,
                    )
                    .order_by(
                        CatalogDocumentRevision.revision_number.desc(),
                        CatalogDocumentRevision.created_at.desc(),
                        CatalogDocumentRevision.id.desc(),
                    )
                    .limit(1)
                )
                revision = next(iter(revisions), None)
                if revision is None:
                    raise ArtifactLifecycleError(
                        "artifact.reference_scan_failed",
                        "saved-profile recipe selector has no readable active revision; removal was deferred",
                        retryable=True,
                    )
                for set_digest, row in sets.items():
                    if row.recipe_revision_sha256 == revision.content_digest:
                        findings[set_digest].add(
                            _finding(
                                "model-set",
                                set_digest,
                                owner_kind="fleet-profile",
                                owner_id=profile.id,
                                state="saved",
                                classification="saved-reference",
                                detail=(
                                    "saved profile assignment resolves to this "
                                    "active recipe revision"
                                ),
                                reason=f"saved profile {profile.id}",
                            )
                        )
    except ArtifactLifecycleError:
        raise
    except Exception as error:
        raise ArtifactLifecycleError(
            "artifact.reference_scan_failed",
            "saved-profile references could not be validated; removal was deferred",
            retryable=True,
        ) from error

    owner_bytes = 0

    def account(value: object) -> None:
        nonlocal owner_bytes
        try:
            owner_bytes += len(canonical_message(value))
        except (TypeError, ValueError) as error:
            raise ArtifactLifecycleError(
                "artifact.reference_scan_failed",
                "accepted reference JSON is malformed; removal was deferred",
                retryable=True,
            ) from error
        if owner_bytes > MAX_ARTIFACT_OWNER_SCAN_BYTES:
            raise ArtifactLifecycleError(
                "artifact.reference_scan_limited",
                f"accepted-reference scan exceeded {MAX_ARTIFACT_OWNER_SCAN_BYTES} bytes; removal was deferred",
                retryable=True,
            )

    for application in session.scalars(
        select(FleetProfileApplication)
        .where(FleetProfileApplication.state.in_(_ACTIVE_PROFILE_APPLICATIONS))
        .order_by(FleetProfileApplication.id)
    ):
        account(application.plan)
        plan = _profile_plan(application.plan)
        for preparation in plan.preparation_decisions:
            identity = preparation.model
            if identity.artifact_set_sha256 in selected:
                findings[identity.artifact_set_sha256].add(
                    _finding(
                        "model-set",
                        identity.artifact_set_sha256,
                        owner_kind="fleet-profile-application",
                        owner_id=application.id,
                        state=application.state,
                        classification="active-work",
                        detail="accepted profile application prepares this model set",
                        reason=f"profile application {application.id}",
                    )
                )

    run_switch_kinds = _run_switch_kinds()
    if run_switch_kinds:
        for operation in session.scalars(
            select(Job)
            .where(
                Job.kind.in_(run_switch_kinds),
                Job.state.in_(_ACTIVE_RUN_SWITCH_JOBS),
            )
            .order_by(Job.id)
        ):
            account(operation.payload)
            plan = _run_switch_plan(operation.payload)
            set_digest = plan.storage.artifact_set_sha256
            if set_digest in selected:
                findings[set_digest].add(
                    _finding(
                        "model-set",
                        set_digest,
                        owner_kind="run-switch-operation",
                        owner_id=operation.id,
                        state=operation.state,
                        classification="active-work",
                        detail="accepted Run/Switch operation uses this model set",
                        reason=f"run/switch operation {operation.id}",
                    )
                )

    for installation in session.scalars(
        select(RecipeInstallation)
        .where(RecipeInstallation.state.in_(_ACTIVE_INSTALLATIONS))
        .order_by(RecipeInstallation.id)
    ):
        account(installation.plan)
        plan = _run_switch_plan({"plan": installation.plan})
        set_digest = plan.storage.artifact_set_sha256
        if set_digest in selected:
            findings[set_digest].add(
                _finding(
                    "model-set",
                    set_digest,
                    owner_kind="recipe-installation",
                    owner_id=installation.id,
                    state=installation.state,
                    classification="active-work",
                    detail="accepted installation uses this model set",
                    reason=f"installation {installation.id}",
                )
            )

    for distribution in session.scalars(
        select(ArtifactDistributionAssignment)
        # Expiry does not prove that a serving worker has stopped. Explicit
        # revocation is the existing durable fence for this reference owner.
        .where(ArtifactDistributionAssignment.state.in_(("active", "expired")))
        .order_by(ArtifactDistributionAssignment.id)
    ):
        account(distribution.objects)
        if distribution.model_artifact_set_sha256 in selected:
            findings[distribution.model_artifact_set_sha256].add(
                _finding(
                    "model-set",
                    distribution.model_artifact_set_sha256,
                    owner_kind="artifact-distribution-assignment",
                    owner_id=distribution.id,
                    state=distribution.state,
                    classification="active-work",
                    detail="accepted distribution assignment retains the model set",
                    reason=f"active distribution assignment {distribution.id}",
                )
            )

    for operation in session.scalars(
        select(ModelCacheOperation)
        .where(
            ModelCacheOperation.kind.in_(("download", "repair")),
            ModelCacheOperation.state.in_(("queued", "running", "partial")),
        )
        .order_by(ModelCacheOperation.id)
    ):
        if operation.artifact_set_sha256 in selected:
            account(operation.payload)
            findings[operation.artifact_set_sha256].add(
                _finding(
                    "model-set",
                    operation.artifact_set_sha256,
                    owner_kind="model-cache-operation",
                    owner_id=operation.id,
                    state=operation.state,
                    classification="active-work",
                    detail="active model-cache download or repair owns this set",
                    reason=f"model-cache operation {operation.id}",
                )
            )

    return {
        digest: tuple(
            sorted(
                value,
                key=lambda item: (
                    item.classification,
                    item.owner_kind,
                    item.owner_id,
                    item.state,
                    item.detail,
                    item.reason,
                ),
            )
        )
        for digest, value in findings.items()
    }


def model_set_reference_reasons(
    session: Session, set_digests: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """Project typed model-set findings into existing presentation reasons."""

    return _reason_projection(model_set_reference_findings(session, set_digests))


def runtime_image_reference_findings(
    session: Session, archive_digests: Iterable[str]
) -> dict[str, tuple[ArtifactReferenceFinding, ...]]:
    """Find typed saved-profile and active owners of exact runtime-image bytes."""

    selected = set(archive_digests)
    findings: dict[str, set[ArtifactReferenceFinding]] = {
        digest: set() for digest in selected
    }
    if not selected:
        return {}

    # Saved selectors protect authorized current-head images, but the grant
    # alone is not a reference. The profile selector plus that grant resolves
    # to the exact archive identity.
    profile_bytes = 0
    owner_bytes = 0
    try:
        for profile in session.scalars(select(FleetProfile).order_by(FleetProfile.id)):
            encoded = canonical_message(profile.assignments)
            profile_bytes += len(encoded)
            if profile_bytes > _PROFILE_PAYLOAD_BUDGET:
                raise ArtifactLifecycleError(
                    "artifact.reference_scan_limited",
                    f"saved-profile reference scan exceeded {_PROFILE_PAYLOAD_BUDGET} bytes; removal was deferred",
                    retryable=True,
                )
            assignments = TypeAdapter(list[FleetProfileAssignmentInput]).validate_json(
                encoded, strict=True
            )
            for assignment in assignments:
                publisher, separator, slug = assignment.recipe_selector.partition("/")
                if not separator:
                    raise ValueError("stored recipe selector is invalid")
                revision = session.scalar(
                    select(CatalogDocumentRevision)
                    .where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.state == "active",
                        CatalogDocumentRevision.publisher == publisher,
                        CatalogDocumentRevision.slug == slug,
                    )
                    .order_by(
                        CatalogDocumentRevision.revision_number.desc(),
                        CatalogDocumentRevision.created_at.desc(),
                        CatalogDocumentRevision.id.desc(),
                    )
                    .limit(1)
                )
                if revision is None:
                    raise ArtifactLifecycleError(
                        "artifact.reference_scan_failed",
                        "saved-profile recipe selector has no readable active revision; removal was deferred",
                        retryable=True,
                    )
                for authorization in session.scalars(
                    select(RuntimeImageAuthorization).where(
                        RuntimeImageAuthorization.recipe_revision_id == revision.id,
                        RuntimeImageAuthorization.state == "authorized",
                        RuntimeImageAuthorization.oci_archive_sha256.in_(selected),
                    )
                ):
                    findings[authorization.oci_archive_sha256].add(
                        _finding(
                            "runtime-image",
                            authorization.oci_archive_sha256,
                            owner_kind="fleet-profile",
                            owner_id=profile.id,
                            state="saved",
                            classification="saved-reference",
                            detail=(
                                "saved profile assignment resolves to this "
                                "authorized active recipe image"
                            ),
                            reason=f"saved profile {profile.id}",
                        )
                    )
    except ArtifactLifecycleError:
        raise
    except Exception as error:
        raise ArtifactLifecycleError(
            "artifact.reference_scan_failed",
            "saved-profile references could not be validated; removal was deferred",
            retryable=True,
        ) from error

    def account(value: object) -> None:
        nonlocal owner_bytes
        try:
            owner_bytes += len(canonical_message(value))
        except (TypeError, ValueError) as error:
            raise ArtifactLifecycleError(
                "artifact.reference_scan_failed",
                "accepted reference JSON is malformed; removal was deferred",
                retryable=True,
            ) from error
        if owner_bytes > MAX_ARTIFACT_OWNER_SCAN_BYTES:
            raise ArtifactLifecycleError(
                "artifact.reference_scan_limited",
                f"accepted-reference scan exceeded {MAX_ARTIFACT_OWNER_SCAN_BYTES} bytes; removal was deferred",
                retryable=True,
            )

    for application in session.scalars(
        select(FleetProfileApplication)
        .where(FleetProfileApplication.state.in_(_ACTIVE_PROFILE_APPLICATIONS))
        .order_by(FleetProfileApplication.id)
    ):
        account(application.plan)
        for preparation in _profile_plan(application.plan).preparation_decisions:
            archive = preparation.runtime_image.oci_layout_sha256
            if archive in selected:
                findings[archive].add(
                    _finding(
                        "runtime-image",
                        archive,
                        owner_kind="fleet-profile-application",
                        owner_id=application.id,
                        state=application.state,
                        classification="active-work",
                        detail="accepted profile application prepares this runtime image",
                        reason=f"profile application {application.id}",
                    )
                )

    run_switch_kinds = _run_switch_kinds()
    if run_switch_kinds:
        for operation in session.scalars(
            select(Job)
            .where(
                Job.kind.in_(run_switch_kinds),
                Job.state.in_(_ACTIVE_RUN_SWITCH_JOBS),
            )
            .order_by(Job.id)
        ):
            account(operation.payload)
            account(operation.result)
            plan = _run_switch_plan(operation.payload)
            archive = plan.runtime_storage.oci_layout_sha256
            if archive in selected:
                findings[archive].add(
                    _finding(
                        "runtime-image",
                        archive,
                        owner_kind="run-switch-operation",
                        owner_id=operation.id,
                        state=operation.state,
                        classification="active-work",
                        detail="accepted Run/Switch operation uses this runtime image",
                        reason=f"run/switch operation {operation.id}",
                    )
                )
            intent = _run_switch_runtime_image_intent(operation, plan)
            if intent is not None and intent.archive_sha256 in selected:
                findings[intent.archive_sha256].add(
                    _finding(
                        "runtime-image",
                        intent.archive_sha256,
                        owner_kind="run-switch-operation",
                        owner_id=operation.id,
                        state=operation.state,
                        classification="active-work",
                        detail="accepted Run/Switch operation is preparing this runtime image",
                        reason=(
                            f"run/switch operation {operation.id} "
                            "runtime image preparation"
                        ),
                    )
                )

    for installation in session.scalars(
        select(RecipeInstallation)
        .where(RecipeInstallation.state.in_(_ACTIVE_INSTALLATIONS))
        .order_by(RecipeInstallation.id)
    ):
        account(installation.plan)
        archive = _run_switch_plan(
            {"plan": installation.plan}
        ).runtime_storage.oci_layout_sha256
        if archive in selected:
            findings[archive].add(
                _finding(
                    "runtime-image",
                    archive,
                    owner_kind="recipe-installation",
                    owner_id=installation.id,
                    state=installation.state,
                    classification="active-work",
                    detail="accepted installation uses this runtime image",
                    reason=f"installation {installation.id}",
                )
            )

    for distribution in session.scalars(
        select(ArtifactDistributionAssignment)
        # Expiry is only a serving deadline; it does not fence a stale worker.
        .where(ArtifactDistributionAssignment.state.in_(("active", "expired")))
        .order_by(ArtifactDistributionAssignment.id)
    ):
        if distribution.oci_archive_sha256 in selected:
            findings[distribution.oci_archive_sha256].add(
                _finding(
                    "runtime-image",
                    distribution.oci_archive_sha256,
                    owner_kind="artifact-distribution-assignment",
                    owner_id=distribution.id,
                    state=distribution.state,
                    classification="active-work",
                    detail="accepted distribution assignment retains the runtime image",
                    reason=f"active distribution assignment {distribution.id}",
                )
            )

    for run in session.scalars(
        select(RecipeRun)
        .where(RecipeRun.state.in_(_ACTIVE_RUNS))
        .order_by(RecipeRun.id)
    ):
        installation = session.get(RecipeInstallation, run.installation_id)
        if installation is None:
            raise ArtifactLifecycleError(
                "artifact.reference_scan_failed",
                "active run installation could not be read; removal was deferred",
                retryable=True,
            )
        account(installation.plan)
        archive = _run_switch_plan(
            {"plan": installation.plan}
        ).runtime_storage.oci_layout_sha256
        if archive in selected:
            findings[archive].add(
                _finding(
                    "runtime-image",
                    archive,
                    owner_kind="recipe-run",
                    owner_id=run.id,
                    state=run.state,
                    classification="active-work",
                    detail="active recipe run retains its installed runtime image",
                    reason=f"active run {run.id}",
                )
            )

    from .recipe_image_availability import OPERATION_KIND

    image_revisions = {
        digest: set(
            session.scalars(
                select(RuntimeImageAuthorization.recipe_revision_id).where(
                    RuntimeImageAuthorization.oci_archive_sha256 == digest
                )
            )
        )
        for digest in selected
    }

    for operation in session.scalars(
        select(Job)
        .where(
            Job.kind == OPERATION_KIND,
            Job.state.in_(_ACTIVE_ARTIFACT_JOBS),
        )
        .order_by(Job.id)
    ):
        payload = operation.payload
        if not isinstance(payload, Mapping):
            raise ArtifactLifecycleError(
                "artifact.reference_scan_failed",
                "active artifact operation payload is malformed; removal was deferred",
                retryable=True,
            )
        associated_archives = [
            archive
            for archive, revisions in image_revisions.items()
            if operation.authority_revision in revisions
        ]
        if associated_archives:
            account(payload)
            for archive in associated_archives:
                findings[archive].add(
                    _finding(
                        "runtime-image",
                        archive,
                        owner_kind="recipe-image-availability-operation",
                        owner_id=operation.id,
                        state=operation.state,
                        classification="active-work",
                        detail="active image preparation is authorized for this revision",
                        reason=(
                            f"active image preparation {operation.id} "
                            "for recipe revision"
                        ),
                    )
                )
        raw_reference = payload.get("image_reference_intent")
        if raw_reference is not None:
            try:
                reference = read_runtime_image_reference_intent(raw_reference)
            except RuntimeImagePreparationError as error:
                raise ArtifactLifecycleError(
                    "artifact.reference_scan_failed",
                    "active runtime image reference intent is malformed; removal was deferred",
                    retryable=True,
                ) from error
            claim_owner = payload.get("claim_owner")
            if not isinstance(claim_owner, str) or not reference.belongs_to(
                operation_id=operation.id,
                recipe_revision_id=operation.authority_revision,
                attempt=operation.current_attempt,
                claim_owner=claim_owner,
            ):
                raise ArtifactLifecycleError(
                    "artifact.reference_scan_failed",
                    "active runtime image reference intent is not owned by its current attempt; removal was deferred",
                    retryable=True,
                )
            account(reference)
            if reference.oci_archive_sha256 in selected:
                findings[reference.oci_archive_sha256].add(
                    _finding(
                        "runtime-image",
                        reference.oci_archive_sha256,
                        owner_kind="recipe-image-availability-operation",
                        owner_id=operation.id,
                        state=operation.state,
                        classification="active-work",
                        detail="active image operation has an exact publication intent",
                        reason=f"image publication operation {operation.id}",
                    )
                )
        image_result = payload.get("image_result")
        if isinstance(image_result, Mapping):
            archive = image_result.get("oci_archive_sha256")
            if isinstance(archive, str) and archive in selected:
                account(image_result)
                findings[archive].add(
                    _finding(
                        "runtime-image",
                        archive,
                        owner_kind="recipe-image-availability-operation",
                        owner_id=operation.id,
                        state=operation.state,
                        classification="active-work",
                        detail="active image operation has a verified publication result",
                        reason=f"image operation {operation.id}",
                    )
                )

    return {
        digest: tuple(
            sorted(
                value,
                key=lambda item: (
                    item.classification,
                    item.owner_kind,
                    item.owner_id,
                    item.state,
                    item.detail,
                    item.reason,
                ),
            )
        )
        for digest, value in findings.items()
    }


def runtime_image_reference_reasons(
    session: Session, archive_digests: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """Project typed runtime-image findings into existing presentation reasons."""

    return _reason_projection(
        runtime_image_reference_findings(session, archive_digests)
    )


def _profile_plan(value: object) -> FleetProfilePreview:
    try:
        return FleetProfilePreview.model_validate_json(
            canonical_message(value), strict=True
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ArtifactLifecycleError(
            "artifact.reference_scan_failed",
            "accepted profile plan is malformed; removal was deferred",
            retryable=True,
        ) from error


def _run_switch_plan(payload: Mapping[str, object]) -> RunSwitchPlan:
    try:
        return RunSwitchPlan.model_validate_json(
            canonical_message(payload.get("plan")), strict=True
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ArtifactLifecycleError(
            "artifact.reference_scan_failed",
            "accepted Run/Switch plan is malformed; removal was deferred",
            retryable=True,
        ) from error


def _run_switch_runtime_image_intent(
    operation: Job, plan: RunSwitchPlan
) -> RunSwitchRuntimeImageReferenceIntent | None:
    """Read one active job's strict, exact prepublication artifact reference."""

    if operation.result is None:
        return None
    try:
        result = RunSwitchOperationResult.model_validate_json(
            canonical_message(operation.result), strict=True
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ArtifactLifecycleError(
            "artifact.reference_scan_failed",
            "active RunSwitch progress is malformed; image removal was deferred",
            retryable=True,
        ) from error
    intent = result.runtime_image_reference_intent
    if intent is None:
        return None
    phase = (
        plan.phases[intent.phase_index]
        if intent.phase_index < len(plan.phases)
        else None
    )
    target_nodes = tuple(sorted(node.node_id for node in plan.spark_group.nodes))
    expected_build_id = plan.recipe_build_id or plan.build.build_id
    valid = (
        operation.kind == "recipe.run-switch.v2"
        and intent.owner_kind == "run-switch-job"
        and intent.operation_id == operation.id
        and intent.request_key == operation.request_id
        and intent.actor == operation.actor
        and intent.plan_digest == operation.payload.get("plan_digest")
        and intent.plan_digest == plan.plan_digest
        and tuple(sorted(operation.targets)) == target_nodes
        and operation.payload.get("workload_intent_ordinal")
        == intent.workload_intent_ordinal
        and result.workload_intent_ordinal == intent.workload_intent_ordinal
        and result.profile_application_id == intent.profile_application_id
        and intent.recipe_revision_id == plan.recipe_revision_id
        and phase is not None
        and phase.kind == "prepare"
        and phase.subphase == "runtime-image"
        and intent.item_index == 0
        and (
            plan.image_digest is None
            or plan.image_digest
            in {intent.image_digest, intent.registry_manifest_digest}
        )
        and (
            plan.runtime_storage.image_digest is None
            or plan.runtime_storage.image_digest == intent.image_digest
        )
        and (
            plan.runtime_storage.registry_manifest_digest is None
            or plan.runtime_storage.registry_manifest_digest
            == intent.registry_manifest_digest
        )
        and (
            plan.runtime_storage.oci_layout_sha256 is None
            or plan.runtime_storage.oci_layout_sha256 == intent.archive_sha256
        )
        and (
            plan.runtime_storage.image_bytes is None
            or plan.runtime_storage.image_bytes == 0
            or plan.runtime_storage.image_bytes == intent.image_bytes
        )
        and (
            intent.source == "controller-build"
            and intent.build_id == expected_build_id
            and intent.build_input_sha256 == plan.build.build_input_sha256
            and (
                plan.build.image_digest is None
                or plan.build.image_digest == intent.image_digest
            )
            and (
                plan.build.oci_layout_sha256 is None
                or plan.build.oci_layout_sha256 == intent.archive_sha256
            )
            and (
                plan.build.image_bytes is None
                or plan.build.image_bytes == 0
                or plan.build.image_bytes == intent.image_bytes
            )
            or intent.source == "published"
            and expected_build_id is None
        )
    )
    if not valid:
        raise ArtifactLifecycleError(
            "artifact.reference_scan_failed",
            "active RunSwitch image reference does not match its owner plan; image removal was deferred",
            retryable=True,
        )
    return intent


def _run_switch_kinds() -> frozenset[str]:
    from .run_switch_operations import _OPERATION_KINDS

    return frozenset(_OPERATION_KINDS)


__all__ = [
    "MAX_ARTIFACT_OWNER_SCAN_BYTES",
    "ArtifactReferenceFinding",
    "model_set_objects",
    "model_set_reference_findings",
    "model_set_reference_reasons",
    "require_model_sets_open",
    "runtime_image_reference_findings",
    "runtime_image_reference_reasons",
]
