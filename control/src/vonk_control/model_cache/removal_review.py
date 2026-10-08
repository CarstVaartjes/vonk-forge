"""Removal review."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from vonk_agent_protocol import ArtifactLifecycleCode, ModelCacheCode

from .. import model_cache_states
from ..artifact_lifecycle import ArtifactIdentity, ArtifactKind, ArtifactLifecycleError
from ..artifact_reference_scan import model_set_objects, model_set_reference_findings
from ..cache_removal_review import (
    CacheRemovalBlocker,
    CacheRemovalFinding,
    CacheRemovalReview,
    CacheRemovalReviewContent,
    refusing_removal_blockers,
    seal_cache_removal_review,
)
from ..categorized_errors import InvalidValue
from ..lifecycle.evidence import Residue
from ..model_cache_contract import ModelCacheRemovalPayload
from ..models import (
    ArtifactLifecycleGate,
    CatalogDocumentRevision,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
)
from .catalog_helpers import _recipe_model_content_digests
from .errors import (
    ModelCacheConflictRefused,
    ModelCacheConflictUnknown,
    ModelCacheRemovalOwnerInvalid,
    ModelCacheStorageUnknown,
)
from .persistence import _operation_removal
from .source_helpers import _model_selector, _request_key
from .views import CacheOperationView, ModelCacheRemovalScope

if TYPE_CHECKING:
    from .service import ModelCacheService


class RemovalReviewMixin:
    """Removal review behavior of the cache service."""

    def download_model_selector(
        self,
        selector: str,
        *,
        actor: str,
        request_key: str,
        force: bool = False,
    ) -> CacheOperationView:
        """Plan and queue a model download from the operator selector."""
        cache = cast("ModelCacheService", self)

        request_key = _request_key(request_key)
        selector = _model_selector(selector)
        with cache._session() as session:
            replay = cache._download_replay(
                session, request_key, actor=actor, selector=selector, force=force
            )
            if replay is not None:
                return replay
        digest = cache._resolve_model_selector(selector)
        manifest = cache.resolve_artifact_set(model_content_sha256=digest)
        preview = cache._download_preview_for_manifest(manifest)
        # A capacity blocker is a wait, not a refusal: start_download queues the
        # operation, which is claimed once the space it needs exists.
        return cache.start_download(
            actor=actor,
            request_key=request_key,
            plan_digest=str(preview["plan_digest"]),
            artifact_set_sha256=manifest.digest,
            model_content_sha256=digest,
            selector=selector,
            force=force,
        )

    def remove_model_selector(
        self,
        selector: str,
        *,
        actor: str,
        request_key: str,
    ) -> CacheOperationView:
        """Accept a durable removal of the named model against current state.

        The removal applies to what the selector resolves to now. Sets still in
        use are fenced against new consumers and the removal waits for their
        current owners.
        """
        cache = cast("ModelCacheService", self)

        request_key = _request_key(request_key)
        normalized_selector = _model_selector(selector).casefold()
        with cache._session() as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                return cache._replay_model_removal(
                    existing, actor=actor, selector=normalized_selector
                )

        cache.reconcile_removal_gates()
        reviewed = cache.review_model_removal(normalized_selector)
        blockers = refusing_removal_blockers(reviewed)
        if blockers:
            first = blockers[0]
            if first.retryable:
                raise ModelCacheConflictUnknown(
                    first.code,
                    first.detail,
                    recovery="retry",
                )
            raise ModelCacheConflictRefused(first.code, first.detail)

        try:
            with cache._lock, cache._session(write=True) as session:
                existing = session.scalar(
                    select(ModelCacheOperation).where(
                        ModelCacheOperation.request_key == request_key
                    )
                )
                if existing is not None:
                    return cache._replay_model_removal(
                        existing, actor=actor, selector=normalized_selector
                    )
                digest = cache._resolve_model_selector_in_session(
                    session, normalized_selector
                )
                operation = cache._accept_model_removal(
                    session,
                    actor=actor,
                    request_key=request_key,
                    selector=normalized_selector,
                    model_content_sha256=digest,
                    selected_sets=None,
                    review_digest=reviewed.review_digest,
                )
                operation_id = operation.id
                removal = _operation_removal(operation)
                superseded = cache._cancel_superseded_downloads(
                    session,
                    () if isinstance(removal, Residue) else removal.selected,
                    actor=actor,
                    request_key=request_key,
                )
        except IntegrityError:
            # The unique request key arbitrates first submission across
            # Controller processes.  Resolve the winner only after rollback.
            replay = cache._model_removal_by_request(
                request_key, actor=actor, selector=normalized_selector
            )
            if replay is None:
                raise
            return replay
        for superseded_id in superseded:
            cache.signal_cancelled_operation(superseded_id)
        return cache.get_operation(operation_id)

    def accept_unused_removal(
        self,
        model_content_sha256: str,
        *,
        actor: str,
        request_key: str,
        verify: Callable[[Session, tuple[str, ...]], None],
    ) -> CacheOperationView:
        """Accept the removal of a model nobody asked to remove.

        The same durable, fenced removal as a request-led one, with one
        difference: ``verify`` is called with every set and object gate held,
        before the removal is accepted, and refuses it by raising. A sweep
        therefore never fences a model that a load reached after it looked, and
        never leaves a fence behind that waits for a current owner.
        """
        cache = cast("ModelCacheService", self)

        request_key = _request_key(request_key)
        with cache._lock, cache._session(write=True) as session:
            operation = cache._accept_model_removal(
                session,
                actor=actor,
                request_key=request_key,
                selector=model_content_sha256,
                model_content_sha256=model_content_sha256,
                selected_sets=None,
                verify=verify,
            )
            operation_id = operation.id
        return cache.get_operation(operation_id)

    def accept_unused_set_removal(
        self,
        set_digest: str,
        *,
        actor: str,
        request_key: str,
        verify: Callable[[Session, tuple[str, ...]], None],
    ) -> CacheOperationView:
        """Remove an abandoned exact set even if its catalog model is gone."""
        cache = cast("ModelCacheService", self)
        request_key = _request_key(request_key)
        with cache._lock, cache._session(write=True) as session:
            operation = cache._accept_model_removal(
                session,
                actor=actor,
                request_key=request_key,
                selector=set_digest,
                model_content_sha256=None,
                selected_sets=(set_digest,),
                verify=verify,
            )
            operation_id = operation.id
        return cache.get_operation(operation_id)

    def unused_set_bytes(self, set_digest: str) -> int | None:
        """Measured complete and partial bytes; unknown size defers eviction."""
        cache = cast("ModelCacheService", self)
        with cache._session() as session:
            scope = cache._model_removal_scope_for_sets(session, (set_digest,))
        assets = cache.removal_asset_status(scope)
        available = [
            asset.available_bytes for asset in assets if asset.kind == "model-set"
        ]
        return (
            None
            if any(value is None for value in available)
            else sum(value for value in available if value is not None)
        )

    def _cancel_superseded_downloads(
        self,
        session: Session,
        selected_sets: Sequence[str],
        *,
        actor: str,
        request_key: str,
    ) -> tuple[str, ...]:
        """The newer removal intent supersedes older downloads of its sets."""
        cache = cast("ModelCacheService", self)

        if not selected_sets:
            return ()
        cancelled: list[str] = []
        for operation_id in session.scalars(
            select(ModelCacheOperation.id)
            .where(
                ModelCacheOperation.kind == "download",
                ModelCacheOperation.state.in_(model_cache_states.LIVE),
                ModelCacheOperation.artifact_set_sha256.in_(list(selected_sets)),
            )
            .order_by(ModelCacheOperation.id)
        ):
            if cache.cancel_operation_in_session(
                session,
                operation_id,
                actor=actor,
                request_key=str(uuid.uuid5(uuid.UUID(request_key), operation_id)),
                reason="superseded by a newer model removal request",
            ):
                cancelled.append(operation_id)
        return tuple(cancelled)

    def review_model_removal(self, selector: str) -> CacheRemovalReview:
        """Return the current owner-derived model removal impact without writes."""
        cache = cast("ModelCacheService", self)

        normalized_selector = _model_selector(selector).casefold()
        with cache._session() as session:
            digest = cache._resolve_model_selector_in_session(
                session, normalized_selector
            )
            selected_sets = tuple(
                session.scalars(
                    select(ModelCacheSet.artifact_set_sha256)
                    .where(ModelCacheSet.model_content_sha256 == digest)
                    .order_by(ModelCacheSet.artifact_set_sha256)
                )
            )
            scope = cache._model_removal_scope_for_sets(session, selected_sets)
            findings: list[CacheRemovalFinding] = []
            blockers: list[CacheRemovalBlocker] = []
            try:
                with session.begin_nested():
                    by_set = model_set_reference_findings(session, scope.selected_sets)
            except ArtifactLifecycleError as error:
                by_set = {}
                blockers.append(
                    CacheRemovalBlocker(
                        code=error.code,
                        detail=error.detail,
                        retryable=error.retryable,
                        recovery_actions=["retry"] if error.retryable else [],
                    )
                )
            try:
                with session.begin_nested():
                    active_removals = cache.removal_owner_findings_in_session(
                        session, scope
                    )
            except ArtifactLifecycleError as error:
                active_removals = ()
                blockers.append(
                    CacheRemovalBlocker(
                        code=error.code,
                        detail=error.detail,
                        retryable=error.retryable,
                        recovery_actions=["retry"] if error.retryable else [],
                    )
                )
            for entries in by_set.values():
                for entry in entries:
                    finding = CacheRemovalFinding(
                        classification=entry.classification,
                        asset_kind=entry.asset.kind,
                        asset_sha256=entry.asset.sha256,
                        owner_kind=entry.owner_kind,
                        owner_id=entry.owner_id,
                        state=entry.state,
                        detail=entry.detail,
                        reason=entry.reason,
                    )
                    findings.append(finding)
                    blockers.append(
                        CacheRemovalBlocker(
                            code=ModelCacheCode.REMOVAL_REFERENCED,
                            detail=entry.reason,
                            retryable=False,
                            recovery_actions=["resolve_reference"],
                        )
                    )
            findings.extend(active_removals)
            findings.extend(cache.retained_model_object_findings(scope))
            blockers.extend(
                CacheRemovalBlocker(
                    code=ArtifactLifecycleCode.DELETION_IN_PROGRESS,
                    detail=finding.reason,
                    retryable=True,
                    recovery_actions=["observe_removal_operation"],
                )
                for finding in active_removals
            )

        assets = cache.removal_asset_status(scope)
        content = CacheRemovalReviewContent(
            resource_kind="model",
            selector=normalized_selector,
            target_identity=digest,
            with_model=None,
            assets=list(assets),
            references=[
                item for item in findings if item.classification == "saved-reference"
            ],
            active_work=[
                item for item in findings if item.classification == "active-work"
            ],
            blockers=blockers,
            observed_at=cache._clock().isoformat(),
        )
        review = seal_cache_removal_review(content)
        return review

    def removal_owner_findings_in_session(
        self, session: Session, scope: ModelCacheRemovalScope
    ) -> tuple[CacheRemovalFinding, ...]:
        """Project the exact accepted removal owners fencing this model scope.

        The lifecycle gate is the authority for a live deletion fence. Each
        owner is then validated against its durable typed operation intent so
        stale or malformed gate rows remain unknown instead of disappearing from
        review output.
        """

        identities = {("model-set", digest) for digest in scope.selected_sets} | {
            ("model-object", digest) for digest in scope.delete_objects
        }
        if not identities:
            return ()
        digests = {digest for _kind, digest in identities}
        gates = tuple(
            session.scalars(
                select(ArtifactLifecycleGate)
                .where(
                    ArtifactLifecycleGate.artifact_kind.in_(
                        ("model-set", "model-object")
                    ),
                    ArtifactLifecycleGate.artifact_sha256.in_(digests),
                    ArtifactLifecycleGate.removal_owner_id.is_not(None),
                )
                .order_by(
                    ArtifactLifecycleGate.artifact_kind,
                    ArtifactLifecycleGate.artifact_sha256,
                )
            )
        )
        selected_gates = [
            gate
            for gate in gates
            if (gate.artifact_kind, gate.artifact_sha256) in identities
        ]
        findings: list[CacheRemovalFinding] = []
        owners: dict[str, tuple[ModelCacheOperation, ModelCacheRemovalPayload]] = {}
        for gate in selected_gates:
            owner_id = gate.removal_owner_id
            if (
                gate.removal_owner_kind != "model-cache-operation"
                or not isinstance(owner_id, str)
                or not owner_id
                or not isinstance(gate.removal_fence, str)
                or not gate.removal_fence
            ):
                raise ModelCacheRemovalOwnerInvalid(
                    ArtifactLifecycleCode.REMOVAL_OWNER_UNRESOLVED,
                    "a selected cache identity has an unreadable removal owner",
                    retryable=True,
                )
            cached = owners.get(owner_id)
            if cached is None:
                operation = session.get(ModelCacheOperation, owner_id)
                if operation is None or operation.kind != "remove":
                    raise ModelCacheRemovalOwnerInvalid(
                        ArtifactLifecycleCode.REMOVAL_OWNER_UNRESOLVED,
                        "a selected cache identity has no readable removal operation owner",
                        retryable=True,
                    )
                payload = _operation_removal(operation)
                if isinstance(payload, Residue):
                    raise ModelCacheRemovalOwnerInvalid(
                        ArtifactLifecycleCode.REMOVAL_OWNER_INVALID,
                        "a selected cache identity has a malformed removal owner",
                        retryable=True,
                    )
                if payload.removal_fence != gate.removal_fence:
                    raise ModelCacheRemovalOwnerInvalid(
                        ArtifactLifecycleCode.REMOVAL_OWNER_INVALID,
                        "a selected cache identity removal fence disagrees with its owner",
                        retryable=True,
                    )
                cached = (operation, payload)
                owners[owner_id] = cached
            operation, payload = cached
            expected_target = (
                payload.selected
                if gate.artifact_kind == "model-set"
                else payload.delete_objects
            )
            if gate.artifact_sha256 not in expected_target:
                raise ModelCacheRemovalOwnerInvalid(
                    ArtifactLifecycleCode.REMOVAL_OWNER_INVALID,
                    "a selected cache identity is not covered by its stored removal plan",
                    retryable=True,
                )
            if operation.state not in model_cache_states.LIVE:
                raise ModelCacheRemovalOwnerInvalid(
                    ArtifactLifecycleCode.REMOVAL_OWNER_UNRESOLVED,
                    "a selected cache identity remains fenced by a non-active removal owner",
                    retryable=True,
                )
            try:
                identity = ArtifactIdentity(
                    kind=cast(ArtifactKind, gate.artifact_kind),
                    sha256=gate.artifact_sha256,
                )
            except ValueError as error:
                raise ModelCacheRemovalOwnerInvalid(
                    ArtifactLifecycleCode.REMOVAL_OWNER_INVALID,
                    "a selected cache identity has an invalid removal gate",
                    retryable=True,
                ) from error
            identity_kind = identity.kind
            identity_digest = identity.sha256
            reason = (
                f"removal operation {operation.id} is {operation.state} and owns "
                f"{identity_kind} {identity_digest}"
            )
            findings.append(
                CacheRemovalFinding(
                    classification="active-work",
                    asset_kind=identity_kind,
                    asset_sha256=identity_digest,
                    owner_kind="model-cache-operation",
                    owner_id=operation.id,
                    state=model_cache_states.adopted(operation.state),
                    detail="accepted model removal retains this deletion fence",
                    reason=reason,
                )
            )
        return tuple(findings)

    @staticmethod
    def retained_model_object_findings(
        scope: ModelCacheRemovalScope,
    ) -> tuple[CacheRemovalFinding, ...]:
        """Explain retained objects through their exact sibling-set owners."""

        selected_objects = set(scope.selected_objects)
        return tuple(
            CacheRemovalFinding(
                classification="saved-reference",
                asset_kind="model-object",
                asset_sha256=object_digest,
                owner_kind="model-cache-set-membership",
                owner_id=set_digest,
                state=state,
                detail="another cached model set shares this object; removal will retain it",
                reason=f"shared object is retained by model set {set_digest}",
            )
            for object_digest, set_digest, state in scope.shared_memberships
            if object_digest in selected_objects
        )

    def accept_removal_for_sets_in_session(
        self,
        session: Session,
        *,
        actor: str,
        request_key: str,
        selector: str,
        selected_sets: Sequence[str],
    ) -> CacheOperationView:
        """Accept an exact child removal in its recipe parent's transaction."""
        cache = cast("ModelCacheService", self)

        supplied_sets = tuple(selected_sets)
        if not supplied_sets:
            raise InvalidValue("model removal child requires an exact non-empty scope")
        if len(supplied_sets) != len(set(supplied_sets)):
            raise InvalidValue("model removal child scope contains duplicate sets")
        normalized_sets = tuple(sorted(supplied_sets))
        for digest in normalized_sets:
            ArtifactIdentity("model-set", digest)
        operation = cache._accept_model_removal(
            session,
            actor=actor,
            request_key=_request_key(request_key),
            selector=_model_selector(selector),
            model_content_sha256=None,
            selected_sets=normalized_sets,
        )
        return cache._operation_view(operation)

    def accept_recipe_removal_child_in_session(
        self,
        session: Session,
        *,
        actor: str,
        request_key: str,
        recipe_revision_id: str,
        operation_id: str,
        removal_fence: str,
        scope: ModelCacheRemovalScope,
    ) -> tuple[str, str, tuple[str, ...], str] | None:
        """Accept one exact child under gates already reserved by its parent.

        The parent reserves this child's complete identity scope together
        with its own image gates in common kind/digest order. This method
        revalidates that scope and scans protective references in the same
        transaction before it writes the child operation owner.
        """
        cache = cast("ModelCacheService", self)
        if not scope.selected_sets:
            return None
        normalized_key = _request_key(request_key)
        operation = cache._accept_model_removal(
            session,
            actor=actor,
            request_key=normalized_key,
            selector=recipe_revision_id,
            model_content_sha256=None,
            selected_sets=scope.selected_sets,
            operation_id=operation_id,
            removal_fence=removal_fence,
            gates_reserved=True,
            expected_scope=scope,
        )
        payload = _operation_removal(operation)
        if isinstance(payload, Residue) or not isinstance(operation.plan_digest, str):
            raise ModelCacheStorageUnknown(
                ModelCacheCode.REMOVAL_PLAN_INVALID,
                "model removal child has no readable immutable plan",
            )
        accepted_sets = tuple(payload.selected)
        return operation.id, operation.request_key, accepted_sets, operation.plan_digest

    def recipe_removal_scope_in_session(
        self, session: Session, *, recipe_revision_id: str
    ) -> ModelCacheRemovalScope | None:
        """Resolve the exact currently cached model scope for a recipe revision."""
        cache = cast("ModelCacheService", self)

        recipe, _resolved_id, _recipe_digest = cache._recipe_document(
            session, None, recipe_revision_id
        )
        dependency_digests: set[str] = set()
        rows: dict[str, CatalogDocumentRevision] = {}
        for digest in _recipe_model_content_digests(recipe):
            cache._collect_model_definitions(session, digest, rows)
        dependency_digests.update(rows)
        if not dependency_digests:
            return None
        selected_sets = tuple(
            session.scalars(
                select(ModelCacheSet.artifact_set_sha256)
                .where(ModelCacheSet.model_content_sha256.in_(dependency_digests))
                .order_by(ModelCacheSet.artifact_set_sha256)
            )
        )
        if not selected_sets:
            return None
        return cache._model_removal_scope_for_sets(session, selected_sets)

    @staticmethod
    def _model_removal_scope_for_sets(
        session: Session, selected_sets: Sequence[str]
    ) -> ModelCacheRemovalScope:
        normalized_sets = tuple(sorted(set(selected_sets)))
        objects_by_set = model_set_objects(session, normalized_sets)
        selected_objects = tuple(
            sorted({digest for values in objects_by_set.values() for digest in values})
        )
        shared_memberships = tuple(
            session.execute(
                select(
                    ModelCacheSetArtifact.artifact_sha256,
                    ModelCacheSetArtifact.artifact_set_sha256,
                    ModelCacheSet.state,
                )
                .join(
                    ModelCacheSet,
                    ModelCacheSet.artifact_set_sha256
                    == ModelCacheSetArtifact.artifact_set_sha256,
                )
                .where(
                    ModelCacheSetArtifact.artifact_sha256.in_(selected_objects),
                    ModelCacheSetArtifact.artifact_set_sha256.not_in(normalized_sets),
                )
                .order_by(
                    ModelCacheSetArtifact.artifact_sha256,
                    ModelCacheSetArtifact.artifact_set_sha256,
                )
            )
        )
        external_memberships = {row[0] for row in shared_memberships}
        return ModelCacheRemovalScope(
            selected_sets=normalized_sets,
            memberships=tuple(sorted(objects_by_set.items())),
            selected_objects=selected_objects,
            delete_objects=tuple(
                digest
                for digest in selected_objects
                if digest not in external_memberships
            ),
            shared_memberships=tuple(
                (object_digest, set_digest, state)
                for object_digest, set_digest, state in shared_memberships
            ),
        )
