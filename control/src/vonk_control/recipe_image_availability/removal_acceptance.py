"""Removal acceptance for exact recipe image availability."""

from __future__ import annotations

import time
import uuid
from datetime import UTC
from typing import TYPE_CHECKING, cast

from sqlalchemy import select, true
from sqlalchemy.exc import DBAPIError, IntegrityError
from vonk_agent_protocol import (
    LifecycleState,
    RecipeImageCode,
)
from vonk_forge_contracts import document_sha256

from .. import job_states
from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    RemovalOwnerKind,
    reserve_removal_owners,
    supersede_removal_nowait,
)
from ..cache_removal_review import (
    refusing_removal_blockers,
)
from ..job_documents import (
    AvailabilityJobPayload,
)
from ..model_cache import (
    ModelCacheConflict,
)
from ..models import (
    ArtifactLifecycleGate,
    CatalogDocumentRevision,
    Job,
    ModelCacheOperation,
)
from ..recipe_image_availability_view_contract import (
    RecipeCacheRemovalStatus,
)
from ..recipe_image_removal_contract import (
    RecipeCacheRemovalCheckpoint,
    RecipeCacheRemovalIntent,
    RecipeCacheRemovalModelChild,
    RecipeCacheRemovalOwner,
    RecipeCacheRemovalPlan,
)
from ..runtime_image_preparation import (
    RuntimeImagePreparationError,
)
from ..stored_json import read_row_column
from ..strict_json import serialize_json_value
from .contracts import (
    OPERATION_KIND,
    REMOVE_OPERATION_KIND,
    SCHEMA_VERSION,
    ModelCacheRemovalCoordinator,
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityInvalid,
)

if TYPE_CHECKING:
    from .service import RecipeImageAvailabilityService


def remove_selector(
    self: RecipeImageAvailabilityService,
    selector: str,
    *,
    actor: str,
    request_id: str,
    with_model: bool = False,
) -> RecipeCacheRemovalStatus:
    """Accept a restart-safe removal of the named recipe against current state.

    Assets still in use are fenced against new consumers and the removal
    waits for their owners.
    """

    if not isinstance(selector, str) or not 1 <= len(selector.strip()) <= 256:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.SELECTOR_INVALID, "recipe selector is required"
        )
    selector = selector.strip().casefold()
    with self._sessions() as session:
        existing = session.scalar(select(Job).where(Job.request_id == request_id))
        if existing is not None:
            return self._replay_removal(
                existing,
                selector=selector,
                actor=actor,
                request_id=request_id,
                with_model=with_model,
            )

    # Bind content targets without reading local receipts or peer membership.
    # The accepted Job owns every subsequent observation and its finite budget.
    operation_id = str(uuid.uuid4())
    try:
        with self._sessions.begin() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is not None:
                return self._replay_removal(
                    existing,
                    selector=selector,
                    actor=actor,
                    request_id=request_id,
                    with_model=with_model,
                )
            selection = self._recipe_removal_selection_in_session(
                session, selector, with_model=False
            )
            now = self._clock()
            now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
            review = self._sealed_recipe_removal_review(
                selector=selector,
                with_model=with_model,
                selection=selection,
                references=(),
                active_work=(),
                blockers=(),
                assets=(),
                now=now,
            )
            intent = RecipeCacheRemovalIntent(
                schema_version=SCHEMA_VERSION,
                kind=REMOVE_OPERATION_KIND,
                action=review.action,
                selector=selector,
                actor=actor,
                request_key=request_id,
                review_digest=review.review_digest,
                recipe_revision_id=selection.revision_id,
                with_model=with_model,
                removal_fence=str(uuid.uuid4()),
            )
            plan = RecipeCacheRemovalPlan(
                schema_version=SCHEMA_VERSION,
                intent=intent,
                image_archives=list(selection.image_archives),
                model_children=[],
            )
            checkpoint = RecipeCacheRemovalCheckpoint(
                schema_version=SCHEMA_VERSION,
                scope_pending=True,
                image_index=0,
                image_pending_bytes=None,
                image_reclaimed_bytes=0,
                model_index=0,
                model_reclaimed_bytes=0,
                retry_attempts=0,
                failure=None,
            )
            owner = RecipeCacheRemovalOwner(
                schema_version=SCHEMA_VERSION, plan=plan, checkpoint=checkpoint
            )
            operation = self._lifecycle.new_job(
                id=operation_id,
                request_id=request_id,
                kind=REMOVE_OPERATION_KIND,
                actor=actor,
                authority_revision=selection.revision_id,
                targets=[],
                payload_digest=self._removal_payload_digest(plan),
                payload=serialize_json_value(owner),
                result=None,
                current_attempt=1,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            session.flush()
            from .persistence import removal_status

            return removal_status(self, operation, owner)
    except IntegrityError:
        # The unique request key arbitrates concurrent admission. Observe the
        # exact winner after this transaction has rolled back, before retrying
        # any effect or changing the accepted intent.
        with self._sessions() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is None:
                raise
            return self._replay_removal(
                existing,
                selector=selector,
                actor=actor,
                request_id=request_id,
                with_model=with_model,
            )


def observe_recipe_removal(
    self: RecipeImageAvailabilityService, operation_id: str
) -> bool:
    """Re-observe managed storage and peer scope under the original owner."""
    with self._sessions() as session:
        operation = session.get(Job, operation_id)
        if operation is None or operation.state not in job_states.words(
            LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.BACKOFF
        ):
            return False
        pinned = self._read_removal_owner(operation)
        revision = session.get(
            CatalogDocumentRevision, pinned.plan.intent.recipe_revision_id
        )
        if revision is None:
            return False
        content_selector = revision.content_digest
    selector = pinned.plan.intent.selector
    actor = pinned.plan.intent.actor
    request_id = pinned.plan.intent.request_key
    with_model = pinned.plan.intent.with_model
    observed_review = self.review_removal(content_selector, with_model=with_model)
    blockers = refusing_removal_blockers(observed_review)
    if blockers:
        first = blockers[0]
        return self._record_recipe_removal_failure(
            operation_id, code=first.code, detail=first.detail, retryable=True
        )
    try:
        with self._sessions.begin() as session:
            selection = self._recipe_removal_selection_in_session(
                session, content_selector, with_model=with_model
            )
            revision_id = selection.revision_id
            image_archives = selection.image_archives
            model_scope = selection.model_scope

            if image_archives != tuple(pinned.plan.image_archives):
                return False
            removal_fence = pinned.plan.intent.removal_fence
            model_operation_id = str(uuid.uuid4())
            model_removal_fence = str(uuid.uuid4())
            image_owner_kind: RemovalOwnerKind = "recipe-image-job"
            assignments: list[tuple[ArtifactIdentity, RemovalOwnerKind, str, str]] = [
                (
                    ArtifactIdentity("runtime-image", archive),
                    image_owner_kind,
                    operation_id,
                    removal_fence,
                )
                for archive in image_archives
            ]
            if model_scope is not None:
                model_owner_kind: RemovalOwnerKind = "model-cache-operation"
                assignments.extend(
                    (
                        ArtifactIdentity("model-set", set_digest),
                        model_owner_kind,
                        model_operation_id,
                        model_removal_fence,
                    )
                    for set_digest in model_scope.selected_sets
                )
                assignments.extend(
                    (
                        ArtifactIdentity("model-object", object_digest),
                        "model-cache-operation",
                        model_operation_id,
                        model_removal_fence,
                    )
                    for object_digest in model_scope.delete_objects
                )
            now = self._clock()
            now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
            reserve_removal_owners(session, assignments, now=now)
            (
                current_selection,
                references,
                active_work,
                current_blockers,
            ) = self._recipe_removal_impact_in_session(
                session,
                content_selector,
                with_model=with_model,
                own_assignments=assignments,
            )
            current_assets = self._review_assets_for_selection(
                current_selection, observed_review.assets
            )
            current_review = self._sealed_recipe_removal_review(
                selector=selector,
                with_model=with_model,
                selection=current_selection,
                references=references,
                active_work=active_work,
                blockers=current_blockers,
                assets=current_assets,
                now=now,
            )
            current_blockers = refusing_removal_blockers(current_review)
            if current_blockers:
                session.rollback()
                return False

            model_children: list[RecipeCacheRemovalModelChild] = []
            if model_scope is not None:
                child_request_key = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"vonk:recipe-remove-model:{request_id}:{revision_id}",
                    )
                )
                accepted_child = cast(
                    ModelCacheRemovalCoordinator, self._model_cache
                ).accept_recipe_removal_child_in_session(
                    session,
                    actor=actor,
                    request_key=child_request_key,
                    recipe_revision_id=revision_id,
                    operation_id=model_operation_id,
                    removal_fence=model_removal_fence,
                    scope=model_scope,
                )
                if accepted_child is None:
                    session.rollback()
                    return False
                child_id, accepted_key, selected_sets, plan_digest = accepted_child
                if (
                    child_id != model_operation_id
                    or accepted_key != child_request_key
                    or selected_sets != model_scope.selected_sets
                ):
                    session.rollback()
                    return False
                model_children.append(
                    RecipeCacheRemovalModelChild(
                        request_key=accepted_key,
                        operation_id=child_id,
                        plan_digest=plan_digest,
                        selected_sets=list(selected_sets),
                    )
                )

            intent = RecipeCacheRemovalIntent(
                schema_version=SCHEMA_VERSION,
                kind=REMOVE_OPERATION_KIND,
                action=pinned.plan.intent.action,
                selector=selector,
                actor=actor,
                request_key=request_id,
                review_digest=current_review.review_digest,
                recipe_revision_id=revision_id,
                with_model=with_model,
                removal_fence=removal_fence,
            )
            plan = RecipeCacheRemovalPlan(
                schema_version=SCHEMA_VERSION,
                intent=intent,
                image_archives=list(image_archives),
                model_children=model_children,
            )
            checkpoint = pinned.checkpoint.model_copy(
                update={"scope_pending": False, "failure": None}
            )
            owner = RecipeCacheRemovalOwner(
                schema_version=SCHEMA_VERSION,
                plan=plan,
                checkpoint=checkpoint,
            )
            operation = session.get(Job, operation_id, with_for_update={"nowait": True})
            if operation is None or operation.state not in job_states.words(
                LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.BACKOFF
            ):
                session.rollback()
                return False
            current = self._read_removal_owner(operation)
            if current != pinned:
                session.rollback()
                return False
            operation.payload = serialize_json_value(owner)
            operation.payload_digest = self._removal_payload_digest(plan)
        return True
    except (ArtifactLifecycleError, ModelCacheConflict, DBAPIError) as error:
        return self._record_recipe_removal_failure(
            operation_id,
            code=getattr(error, "code", RecipeImageCode.REMOVAL_EVIDENCE_UNAVAILABLE),
            detail=getattr(
                error, "detail", "recipe removal database observation unavailable"
            ),
            retryable=True,
        )


def reconcile_requested_removals(
    self: RecipeImageAvailabilityService, *, limit: int = 64
) -> int:
    """Reconcile only the exact deletion dependencies bound at acceptance."""
    # This stored contract also imports the API response types. Resolve it
    # after the service module has loaded, keeping that import seam acyclic.

    with self._sessions() as session:
        accepted_requests = []
        observed_request = False
        for operation in session.scalars(
            select(Job)
            .where(
                Job.kind == OPERATION_KIND,
                Job.state.in_(
                    job_states.words(LifecycleState.QUEUED, LifecycleState.BACKOFF)
                ),
            )
            .where(
                Job.id > self._removal_request_after
                if self._removal_request_after is not None
                else true()
            )
            .order_by(Job.id)
            .limit(limit)
        ):
            observed_request = True
            self._removal_request_after = operation.id
            try:
                payload = read_row_column(operation, "payload")
                if not isinstance(payload, AvailabilityJobPayload):
                    continue
            except (TypeError, ValueError):
                continue
            if payload.cancellation is not None:
                continue
            for gate in session.scalars(
                select(ArtifactLifecycleGate).where(
                    ArtifactLifecycleGate.artifact_kind == "runtime-image",
                    ArtifactLifecycleGate.artifact_sha256.in_(
                        payload.removal_archives or ()
                    ),
                    ArtifactLifecycleGate.removal_owner_kind == "recipe-image-job",
                    ArtifactLifecycleGate.removal_owner_id.is_not(None),
                )
            ):
                accepted_requests.append(
                    (
                        operation.id,
                        ArtifactIdentity("runtime-image", gate.artifact_sha256),
                    )
                )
        if not observed_request:
            self._removal_request_after = None
    changed = 0
    deadline = time.monotonic() + 0.25
    for request_id, identity in accepted_requests[:limit]:
        if time.monotonic() >= deadline:
            break

        def validate(
            requester: ModelCacheOperation | Job,
            remover: ModelCacheOperation | Job,
            fence: str,
            identity: ArtifactIdentity = identity,
        ) -> bool:
            if not isinstance(requester, Job) or not isinstance(remover, Job):
                return False
            if (
                requester.kind != OPERATION_KIND
                or remover.kind != REMOVE_OPERATION_KIND
            ):
                return False
            try:
                payload = read_row_column(requester, "payload")
                if not isinstance(payload, AvailabilityJobPayload):
                    return False
                owner = self._read_removal_owner(remover)
            except (TypeError, ValueError, RecipeImageAvailabilityError):
                return False
            return (
                payload.cancellation is None
                and requester.authority_revision == payload.recipe_revision_id
                and requester.targets == [payload.recipe_revision_id]
                and payload.recipe_content_sha256
                == document_sha256(payload.recipe.model_dump(mode="json"))
                and identity.sha256 in (payload.removal_archives or ())
                and identity.sha256 in owner.plan.image_archives
                and owner.plan.intent.removal_fence == fence
            )

        def cancel(remover: ModelCacheOperation | Job, accepted_id: str) -> None:
            assert isinstance(remover, Job)
            self._lifecycle.supersede_removal(remover, accepted_id, self._clock())

        try:
            with (
                self._storage.publication_lock(identity.sha256),
                self._sessions.begin() as session,
            ):
                changed += int(
                    supersede_removal_nowait(
                        session,
                        identity,
                        owner_kind="recipe-image-job",
                        request_id=request_id,
                        validate=validate,
                        cancel=cancel,
                        now=self._clock(),
                    )
                )
        except (RuntimeImagePreparationError, ArtifactLifecycleError, OSError):
            continue
    return changed
