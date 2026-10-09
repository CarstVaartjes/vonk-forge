"""Artifact reference scan: model sets."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Literal

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    InvalidRequestReason,
    WaitReason,
    canonical_message,
)

from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactReferenceUnsettled,
    lock_reference_gates,
)
from ..categorized_errors import InvalidValue
from ..fleet_profile_contract import (
    FleetProfileAssignmentInput,
    RecipeSelector,
)
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..model_cache_contract import CacheManifest
from ..models import (
    CatalogDocumentHead,
    CatalogDocumentRevision,
    FleetProfile,
    ModelCacheSet,
    ModelCacheSetArtifact,
)
from .types import ArtifactReferenceFinding, _finding


def require_model_sets_open(
    session: Session,
    set_digests: Iterable[str],
    *,
    now: datetime,
    object_digests: Iterable[str] = (),
    allow_pending_removal: bool = False,
) -> dict[str, tuple[str, ...]]:
    """Lock exact set gates, validate memberships, then lock every object gate.

    Call this inside the same short transaction that creates or accepts the
    existing request/profile/distribution owner. Model-set gates are acquired
    first everywhere, followed by object gates in digest order.
    """

    requested = tuple(sorted(set(set_digests)))
    expected_objects = tuple(sorted(set(object_digests)))
    if expected_objects and len(requested) != 1:
        raise InvalidValue(
            "explicit model object identities require exactly one model set",
            reason=InvalidRequestReason.CONFLICT,
        )
    # Validate all caller identities before creating even the first SQL gate.
    set_identities = tuple(
        ArtifactIdentity("model-set", digest) for digest in requested
    )
    expected_identities = tuple(
        ArtifactIdentity("model-object", digest) for digest in expected_objects
    )
    if not requested:
        return {}
    set_rows = lock_reference_gates(
        session,
        set_identities,
        now=now,
    )
    if not allow_pending_removal and any(
        row.removal_owner_id is not None for row in set_rows
    ):
        raise ArtifactReferenceUnsettled(
            ArtifactLifecycleCode.DELETION_IN_PROGRESS,
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
    if (
        expected_objects
        and existing_sets
        and objects_by_set[requested[0]] != expected_objects
    ):
        raise ArtifactReferenceUnsettled(
            ArtifactLifecycleCode.REFERENCE_IDENTITY_MISMATCH,
            "accepted model object identities disagree with the current model-set membership",
            retryable=True,
            reason=WaitReason.SCOPE_CHANGED,
        )
    all_objects = {digest for values in objects_by_set.values() for digest in values}
    all_objects.update(identity.sha256 for identity in expected_identities)
    object_identities = tuple(
        ArtifactIdentity("model-object", digest) for digest in sorted(all_objects)
    )
    object_rows = lock_reference_gates(session, object_identities, now=now)
    if not allow_pending_removal and any(
        row.removal_owner_id is not None for row in object_rows
    ):
        raise ArtifactReferenceUnsettled(
            ArtifactLifecycleCode.DELETION_IN_PROGRESS,
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
        raise ArtifactReferenceUnsettled(
            ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
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
            raise ArtifactReferenceUnsettled(
                ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
                "model-set manifest is malformed; removal was deferred",
                retryable=True,
            ) from error
        expected = {item.key: (item.sha256, item.path) for item in manifest.artifacts}
        observed = {
            item.artifact_key: (item.artifact_sha256, item.path)
            for item in memberships[set_digest]
        }
        if expected != observed:
            raise ArtifactReferenceUnsettled(
                ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
                "model-set membership disagrees with its manifest; removal was deferred",
                retryable=True,
                reason=WaitReason.SCOPE_CHANGED,
            )
        result[set_digest] = tuple(sorted({item[0] for item in observed.values()}))
    return result


def saved_profile_selectors(profile: FleetProfile) -> tuple[str, ...] | None:
    """Recover the protective selector projection independently of draft fields.

    A damaged option, node or variant cannot hide a readable recipe selector.
    An unreadable selector has unknown scope and protects every candidate;
    neither the scan nor its caller edits authoring intent to invent certainty.
    Each collection attempt ends, and the next pass re-reads the owner.
    """
    try:
        assignments = TypeAdapter(list[FleetProfileAssignmentInput]).validate_json(
            canonical_message(profile.assignments), strict=True
        )
        return tuple(assignment.recipe_selector for assignment in assignments)
    except (TypeError, ValueError):
        retire_as_unknown(
            "artifact-reference.recipe-selector",
            profile.id,
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            "saved profile reference projection is being re-observed",
        )
    if not isinstance(profile.assignments, list):
        return None
    selectors: list[str] = []
    for assignment in profile.assignments:
        if not isinstance(assignment, Mapping):
            return None
        try:
            selector = TypeAdapter(RecipeSelector).validate_python(
                assignment.get("recipe_selector"), strict=True
            )
        except (TypeError, ValueError):
            return None
        selectors.append(selector)
    return tuple(selectors)


def _selector_revisions(
    session: Session, selector: str
) -> tuple[CatalogDocumentRevision, ...]:
    publisher, _, slug = selector.partition("/")
    head = session.scalar(
        select(CatalogDocumentHead).where(
            CatalogDocumentHead.kind == "recipe",
            CatalogDocumentHead.publisher == publisher,
            CatalogDocumentHead.slug == slug,
        )
    )
    if head is not None and head.active_revision_id is not None:
        current = session.get(CatalogDocumentRevision, head.active_revision_id)
        if current is not None:
            return (current,)
    # Missing head bookkeeping is recovered conservatively from this exact
    # selector's records, never by substituting a different selector. This is
    # retention evidence only; it neither changes intent nor admits execution.
    return tuple(
        session.scalars(
            select(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.publisher == publisher,
                CatalogDocumentRevision.slug == slug,
            )
            .order_by(CatalogDocumentRevision.created_at, CatalogDocumentRevision.id)
        )
    )


def _protect_saved(
    findings: dict[str, set[ArtifactReferenceFinding]],
    profile: FleetProfile,
    kind: Literal["model-set", "runtime-image"],
    digests: Iterable[str],
) -> None:
    for digest in digests:
        findings[digest].add(
            _finding(
                kind,
                digest,
                owner_kind="fleet-profile",
                owner_id=profile.id,
                state="saved",
                classification="saved-reference",
                detail="saved profile retains this content or its reference is unresolved",
                reason=f"saved profile {profile.id}",
            )
        )
