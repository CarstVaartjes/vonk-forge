"""Removal review for exact recipe image availability."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    AssetAvailability,
    ModelCacheCode,
    RecipeImageCode,
    RuntimeImageCode,
)

from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    RemovalOwnerKind,
    retryable_artifact_database_error,
)
from ..artifact_reference_scan import (
    ArtifactReferenceFinding,
    model_set_reference_findings,
    runtime_image_reference_findings,
)
from ..cache_removal_review import (
    ArtifactKind,
    AssetDisposition,
    CacheRemovalAsset,
    CacheRemovalBlocker,
    CacheRemovalFinding,
    CacheRemovalReview,
    CacheRemovalReviewContent,
    seal_cache_removal_review,
)
from ..content_identity import ImageContent, same_image
from ..model_cache import (
    ModelCacheRemovalScope,
)
from ..models import (
    ArtifactLifecycleGate,
)
from ..operation_contract import (
    AvailabilityRecoveryAction,
)
from ..runtime_image_preparation import (
    RuntimeImagePreparationError,
)
from .contracts import (
    SCHEMA_VERSION,
    ModelCacheRemovalCoordinator,
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityInvalid,
    _iso,
    _RecipeRemovalSelection,
)

if TYPE_CHECKING:
    from .service import RecipeImageAvailabilityService


def _cache_removal_finding(
    finding: ArtifactReferenceFinding,
) -> CacheRemovalFinding:
    return CacheRemovalFinding(
        classification=finding.classification,
        asset_kind=finding.asset.kind,
        asset_sha256=finding.asset.sha256,
        owner_kind=finding.owner_kind,
        owner_id=finding.owner_id,
        state=finding.state,
        detail=finding.detail,
        reason=finding.reason,
    )


def _recipe_removal_impact_in_session(
    self: RecipeImageAvailabilityService,
    session: Session,
    selector: str,
    *,
    with_model: bool,
    own_assignments: Sequence[tuple[ArtifactIdentity, RemovalOwnerKind, str, str]] = (),
) -> tuple[
    _RecipeRemovalSelection,
    tuple[CacheRemovalFinding, ...],
    tuple[CacheRemovalFinding, ...],
    tuple[CacheRemovalBlocker, ...],
]:
    selection = self._recipe_removal_selection_in_session(
        session, selector, with_model=with_model
    )
    expected_owners = {
        identity: (owner_kind, owner_id, fence)
        for identity, owner_kind, owner_id, fence in own_assignments
    }
    image_findings: dict[str, tuple[ArtifactReferenceFinding, ...]] = {}
    model_findings: dict[str, tuple[ArtifactReferenceFinding, ...]] = {}
    model_owner_findings: tuple[CacheRemovalFinding, ...] = ()
    retained_model_findings: tuple[CacheRemovalFinding, ...] = ()
    scan_blockers: list[CacheRemovalBlocker] = []
    # What the model scan needs is known before it runs: an evidence gap is a
    # retryable blocker of this review (the caller observes and asks again),
    # decided here instead of raised out of the scan and caught below.
    model_identities: set[ArtifactIdentity] = set()
    own_model_identities: set[ArtifactIdentity] = set()
    precondition: CacheRemovalBlocker | None = None
    if selection.model_scope is not None:
        model_identities = {
            ArtifactIdentity("model-set", digest)
            for digest in selection.model_scope.selected_sets
        } | {
            ArtifactIdentity("model-object", digest)
            for digest in selection.model_scope.delete_objects
        }
        own_model_identities = {
            identity
            for identity in expected_owners
            if identity.kind in {"model-set", "model-object"}
        }
        # A model scope exists only when the ModelCache is wired (the selection
        # refuses a with-model review without it).
        if own_model_identities and not model_identities <= own_model_identities:
            precondition = CacheRemovalBlocker(
                code=ArtifactLifecycleCode.REMOVAL_OWNER_UNRESOLVED,
                detail="recipe removal holds an incomplete model deletion fence",
                retryable=True,
                recovery_actions=[AvailabilityRecoveryAction.RETRY],
            )
    try:
        if precondition is not None:
            scan_blockers.append(precondition)
        else:
            # Caught owner-scan errors must roll back their PostgreSQL
            # subtransaction before the independent lifecycle-gate scan runs.
            with session.begin_nested():
                image_findings = runtime_image_reference_findings(
                    session, selection.image_archives
                )
                model_findings = (
                    {}
                    if selection.model_scope is None
                    else model_set_reference_findings(
                        session, selection.model_scope.selected_sets
                    )
                )
                if selection.model_scope is not None:
                    model_coordinator = cast(
                        ModelCacheRemovalCoordinator, self._model_cache
                    )
                    retained_model_findings = (
                        model_coordinator.retained_model_object_findings(
                            selection.model_scope
                        )
                    )
                    if not model_identities <= own_model_identities:
                        model_owner_findings = (
                            model_coordinator.removal_owner_findings_in_session(
                                session, selection.model_scope
                            )
                        )
    except ArtifactLifecycleError as error:
        image_findings = {}
        model_findings = {}
        model_owner_findings = ()
        retained_model_findings = ()
        scan_blockers.append(
            CacheRemovalBlocker(
                code=error.code,
                detail=error.detail,
                retryable=error.retryable,
                recovery_actions=(["retry"] if error.retryable else ["inspect"]),
            )
        )
    except DBAPIError as error:
        image_findings = {}
        model_findings = {}
        model_owner_findings = ()
        retained_model_findings = ()
        translated = retryable_artifact_database_error(error)
        scan_blockers.append(
            CacheRemovalBlocker(
                code=(
                    translated.code
                    if translated is not None
                    else ArtifactLifecycleCode.REFERENCE_SCAN_FAILED
                ),
                detail=(
                    translated.detail
                    if translated is not None
                    else "recipe cache reference scan could not be completed; removal was deferred"
                ),
                retryable=True,
                recovery_actions=[AvailabilityRecoveryAction.RETRY],
            )
        )

    all_findings = (
        tuple(
            self._cache_removal_finding(finding)
            for mapping_by_asset in (image_findings, model_findings)
            for findings in mapping_by_asset.values()
            for finding in findings
        )
        + model_owner_findings
        + retained_model_findings
    )
    references = tuple(
        finding
        for finding in all_findings
        if finding.classification == "saved-reference"
    )
    active_work = tuple(
        finding for finding in all_findings if finding.classification == "active-work"
    )
    blockers: list[CacheRemovalBlocker] = []
    blockers.extend(
        CacheRemovalBlocker(
            code=ArtifactLifecycleCode.DELETION_IN_PROGRESS,
            detail=finding.reason,
            retryable=True,
            recovery_actions=["observe_removal_operation"],
        )
        for finding in model_owner_findings
    )
    for kind, findings_by_asset, label, code in (
        (
            "runtime-image",
            image_findings,
            "runtime image",
            RecipeImageCode.REMOVAL_REFERENCED,
        ),
        (
            "model-set",
            model_findings,
            "model cache set",
            ModelCacheCode.REMOVAL_REFERENCED,
        ),
    ):
        reasons = [
            finding.reason
            for values in findings_by_asset.values()
            for finding in values
        ]
        if reasons:
            unique_reasons = sorted(set(reasons))
            blockers.append(
                CacheRemovalBlocker(
                    code=code,
                    detail=(
                        f"{label} references prevent removal "
                        f"({len(unique_reasons)} owner(s)): "
                        + "; ".join(unique_reasons[:4])
                    )[:512],
                    retryable=True,
                    recovery_actions=(
                        ["inspect"]
                        if any(
                            finding.classification == "saved-reference"
                            for values in findings_by_asset.values()
                            for finding in values
                        )
                        else ["retry"]
                    ),
                )
            )

    identity_groups: dict[str, set[str]] = {}
    if selection.image_archives:
        identity_groups["runtime-image"] = set(selection.image_archives)
    gate_conditions = [
        and_(
            ArtifactLifecycleGate.artifact_kind == kind,
            ArtifactLifecycleGate.artifact_sha256.in_(digests),
        )
        for kind, digests in identity_groups.items()
    ]
    gate_owners: list[str] = []
    try:
        with session.begin_nested():
            if gate_conditions:
                for row in session.scalars(
                    select(ArtifactLifecycleGate)
                    .where(or_(*gate_conditions))
                    .order_by(
                        ArtifactLifecycleGate.artifact_kind,
                        ArtifactLifecycleGate.artifact_sha256,
                    )
                ):
                    if row.removal_owner_id is None:
                        continue
                    identity = ArtifactIdentity(
                        cast(ArtifactKind, row.artifact_kind), row.artifact_sha256
                    )
                    expected = expected_owners.get(identity)
                    actual = (
                        row.removal_owner_kind,
                        row.removal_owner_id,
                        row.removal_fence,
                    )
                    if expected != actual:
                        gate_owners.append(
                            f"{row.artifact_kind} {row.artifact_sha256}: "
                            f"{row.removal_owner_kind} {row.removal_owner_id}"
                        )
    except DBAPIError as error:
        gate_owners = []
        translated = retryable_artifact_database_error(error)
        scan_blockers.append(
            CacheRemovalBlocker(
                code=(
                    translated.code
                    if translated is not None
                    else ArtifactLifecycleCode.REFERENCE_SCAN_FAILED
                ),
                detail=(
                    translated.detail
                    if translated is not None
                    else "recipe removal-owner scan could not be completed; removal was deferred"
                ),
                retryable=True,
                recovery_actions=[AvailabilityRecoveryAction.RETRY],
            )
        )
    blockers.extend(scan_blockers)
    if gate_owners:
        blockers.append(
            CacheRemovalBlocker(
                code=ArtifactLifecycleCode.DELETION_IN_PROGRESS,
                detail=(
                    f"{len(gate_owners)} cache identity/identities are reserved "
                    "for another removal: " + "; ".join(sorted(gate_owners)[:4])
                )[:512],
                retryable=True,
                recovery_actions=[AvailabilityRecoveryAction.RETRY],
            )
        )
    return selection, references, active_work, tuple(blockers)


def _runtime_image_removal_assets(
    self: RecipeImageAvailabilityService, selection: _RecipeRemovalSelection
) -> tuple[tuple[CacheRemovalAsset, ...], tuple[CacheRemovalBlocker, ...]]:
    assets: list[CacheRemovalAsset] = []
    blockers: list[CacheRemovalBlocker] = []
    for archive, expected_bytes in selection.image_expected_bytes:
        availability: AssetAvailability = AssetAvailability.UNKNOWN
        available_bytes: int | None = None
        try:
            with self._storage.publication_lock(archive):
                observed_bytes = self._storage.published_archive_bytes(archive)
                if observed_bytes == 0:
                    availability = AssetAvailability.MISSING
                    available_bytes = 0
                elif observed_bytes < expected_bytes:
                    availability = AssetAvailability.PARTIAL
                    available_bytes = observed_bytes
                elif observed_bytes > expected_bytes:
                    blockers.append(
                        CacheRemovalBlocker(
                            code=RuntimeImageCode.ARCHIVE_SIZE_MISMATCH,
                            detail=(
                                f"runtime image {archive} has {observed_bytes} bytes; "
                                f"the authorized size is {expected_bytes}"
                            ),
                            retryable=False,
                            recovery_actions=["inspect"],
                        )
                    )
                else:
                    receipt = self._storage.read_receipt(archive)
                    if same_image(
                        receipt,
                        ImageContent(
                            archive_sha256=archive, image_bytes=expected_bytes
                        ),
                    ):
                        # Managed publication records only verified bytes;
                        # the exact receipt plus regular-file size is the
                        # cheap readiness check for this operator review.
                        availability = AssetAvailability.VERIFIED
                        available_bytes = expected_bytes
                    else:
                        blockers.append(
                            CacheRemovalBlocker(
                                code=RuntimeImageCode.RECEIPT_IDENTITY_CONFLICT,
                                detail=(
                                    f"runtime image {archive} receipt does not "
                                    "match the authorized identity"
                                ),
                                retryable=False,
                                recovery_actions=["inspect"],
                            )
                        )
        except RuntimeImagePreparationError as error:
            blockers.append(
                CacheRemovalBlocker(
                    code=error.code,
                    detail=f"runtime image {archive}: {error.detail}",
                    retryable=error.retryable,
                    recovery_actions=list(error.recovery_actions)
                    or (["retry"] if error.retryable else ["inspect"]),
                )
            )
        assets.append(
            CacheRemovalAsset(
                kind="runtime-image",
                sha256=archive,
                expected_bytes=expected_bytes,
                availability=availability,
                available_bytes=available_bytes,
                disposition="remove",
            )
        )
    return tuple(assets), tuple(blockers)


def _model_removal_assets(
    self: RecipeImageAvailabilityService, scope: ModelCacheRemovalScope | None
) -> tuple[CacheRemovalAsset, ...]:
    if scope is None:
        return ()
    expected: dict[tuple[str, str], str] = {
        ("model-set", digest): "remove" for digest in scope.selected_sets
    }
    expected.update(
        {
            ("model-object", digest): (
                "remove" if digest in scope.delete_objects else "retain-shared"
            )
            for digest in scope.selected_objects
        }
    )

    def unknown() -> tuple[CacheRemovalAsset, ...]:
        # Peer observations cannot authorize any part of an incomplete scope.
        # The sealed review projects these misses to the pending parent's
        # finite observer, which asks the ModelCache authority again.
        return tuple(
            CacheRemovalAsset(
                kind=cast(ArtifactKind, kind),
                sha256=digest,
                expected_bytes=None,
                availability=AssetAvailability.UNKNOWN,
                available_bytes=None,
                disposition=cast(AssetDisposition, disposition),
            )
            for (kind, digest), disposition in sorted(expected.items())
        )

    if self._model_cache is None:
        return unknown()
    try:
        assets = cast(
            ModelCacheRemovalCoordinator, self._model_cache
        ).removal_asset_status(scope)
    except (TypeError, ValueError):
        return unknown()
    if not isinstance(assets, Sequence) or isinstance(assets, (str, bytes)):
        return unknown()
    observed: dict[tuple[str, str], CacheRemovalAsset] = {}
    for asset in assets:
        if not isinstance(asset, CacheRemovalAsset):
            return unknown()
        identity = (asset.kind, asset.sha256)
        if identity in observed or expected.get(identity) != asset.disposition:
            return unknown()
        observed[identity] = asset
    if set(observed) != set(expected):
        return unknown()
    return tuple(observed[key] for key in sorted(observed))


def _review_assets_for_selection(
    selection: _RecipeRemovalSelection,
    observed_assets: Sequence[CacheRemovalAsset],
) -> tuple[CacheRemovalAsset, ...]:
    by_identity = {(asset.kind, asset.sha256): asset for asset in observed_assets}
    expected: dict[tuple[ArtifactKind, str], tuple[int | None, AssetDisposition]] = {
        ("runtime-image", digest): (size, "remove")
        for digest, size in selection.image_expected_bytes
    }
    if selection.model_scope is not None:
        expected.update(
            {
                ("model-set", digest): (None, "remove")
                for digest in selection.model_scope.selected_sets
            }
        )
        expected.update(
            {
                ("model-object", digest): (
                    None,
                    "remove"
                    if digest in selection.model_scope.delete_objects
                    else "retain-shared",
                )
                for digest in selection.model_scope.selected_objects
            }
        )
    assets: list[CacheRemovalAsset] = []
    for identity, (expected_bytes, disposition) in sorted(expected.items()):
        observed = by_identity.get(identity)
        if observed is None or observed.disposition != disposition:
            assets.append(
                CacheRemovalAsset(
                    kind=identity[0],
                    sha256=identity[1],
                    expected_bytes=expected_bytes,
                    availability=AssetAvailability.UNKNOWN,
                    available_bytes=None,
                    disposition=disposition,
                )
            )
        else:
            if expected_bytes is not None and observed.expected_bytes != expected_bytes:
                assets.append(
                    CacheRemovalAsset(
                        kind=identity[0],
                        sha256=identity[1],
                        expected_bytes=expected_bytes,
                        availability=AssetAvailability.UNKNOWN,
                        available_bytes=None,
                        disposition=disposition,
                    )
                )
            else:
                assets.append(observed)
    return tuple(assets)


def _sealed_recipe_removal_review(
    self: RecipeImageAvailabilityService,
    *,
    selector: str,
    with_model: bool,
    selection: _RecipeRemovalSelection,
    references: Sequence[CacheRemovalFinding],
    active_work: Sequence[CacheRemovalFinding],
    blockers: Sequence[CacheRemovalBlocker],
    assets: Sequence[CacheRemovalAsset],
    now: datetime,
) -> CacheRemovalReview:
    known_asset_blockers = {
        asset.sha256
        for asset in assets
        if any(asset.sha256 in blocker.detail for blocker in blockers)
    }
    asset_blockers = [
        CacheRemovalBlocker(
            code=ArtifactLifecycleCode.ASSET_AVAILABILITY_UNKNOWN,
            detail=(f"{asset.kind} {asset.sha256} storage readiness is unknown"),
            retryable=True,
            recovery_actions=[AvailabilityRecoveryAction.RETRY],
        )
        for asset in assets
        if asset.availability == AssetAvailability.UNKNOWN
        and asset.sha256 not in known_asset_blockers
    ]
    blockers_by_identity = {
        (blocker.code, blocker.detail): blocker
        for blocker in (*blockers, *asset_blockers)
    }
    return seal_cache_removal_review(
        CacheRemovalReviewContent(
            schema_version=SCHEMA_VERSION,
            action="remove",
            resource_kind="recipe",
            selector=selector,
            target_identity=selection.revision_id,
            with_model=with_model,
            assets=list(assets),
            references=list(references),
            active_work=list(active_work),
            blockers=list(blockers_by_identity.values()),
            observed_at=_iso(now),
        )
    )


def review_removal(
    self: RecipeImageAvailabilityService, selector: str, *, with_model: bool
) -> CacheRemovalReview:
    """Return one complete read-only review of exact recipe cache effects."""

    if not isinstance(selector, str) or not 1 <= len(selector.strip()) <= 256:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.SELECTOR_INVALID, "recipe selector is required"
        )
    if type(with_model) is not bool:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.REMOVAL_CHOICE_INVALID,
            "with_model must be an explicit boolean",
        )
    normalized = selector.strip().casefold()
    now = self._clock()
    now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    try:
        with self._sessions.begin() as session:
            selection, references, active_work, blockers = (
                self._recipe_removal_impact_in_session(
                    session,
                    normalized,
                    with_model=with_model,
                )
            )
    except (ArtifactLifecycleError, RecipeImageAvailabilityError) as error:
        # A scope gap is part of the review, consumed by the accepted owner's
        # observer; it cannot convert a well-formed removal into a refusal.
        with self._sessions() as session:
            selection = self._recipe_removal_selection_in_session(
                session,
                normalized,
                with_model=False,
            )
        references = ()
        active_work = ()
        blockers = (
            CacheRemovalBlocker(
                code=error.code,
                detail=error.detail,
                retryable=True,
                recovery_actions=[],
            ),
        )
    image_assets, image_blockers = self._runtime_image_removal_assets(selection)
    try:
        model_assets = self._model_removal_assets(selection.model_scope)
    except RecipeImageAvailabilityError as error:
        model_assets = tuple(
            asset
            for asset in self._review_assets_for_selection(selection, ())
            if asset.kind != "runtime-image"
        )
        blockers = (
            *blockers,
            CacheRemovalBlocker(
                code=error.code,
                detail=error.detail,
                retryable=True,
                recovery_actions=[],
            ),
        )
    return self._sealed_recipe_removal_review(
        selector=normalized,
        with_model=with_model,
        selection=selection,
        references=references,
        active_work=active_work,
        blockers=tuple(blockers) + image_blockers,
        assets=image_assets + model_assets,
        now=now,
    )
