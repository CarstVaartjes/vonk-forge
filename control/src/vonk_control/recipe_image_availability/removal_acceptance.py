"""Removal acceptance for exact recipe image availability."""

from __future__ import annotations

import time
import uuid
from datetime import UTC
from typing import TYPE_CHECKING, cast

from sqlalchemy import select, true
from sqlalchemy.exc import IntegrityError
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    ModelCacheCode,
    RecipeImageCode,
    WaitReason,
    canonical_message,
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
from ..artifact_reference_scan import (
    MAX_ARTIFACT_OWNER_SCAN_BYTES,
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
    RecipeImageAvailabilityRefused,
    RecipeImageAvailabilityUnknown,
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

    observed_review = self.review_removal(selector, with_model=with_model)
    observed_blockers = refusing_removal_blockers(observed_review)
    if observed_blockers:
        first_blocker = observed_blockers[0]
        raise RecipeImageAvailabilityRefused(
            first_blocker.code,
            first_blocker.detail,
            retryable=first_blocker.retryable,
            recovery_actions=first_blocker.recovery_actions,
        )

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
                session, selector, with_model=with_model
            )
            revision_id = selection.revision_id
            image_archives = selection.image_archives
            model_scope = selection.model_scope

            removal_fence = str(uuid.uuid4())
            model_operation_id = str(uuid.uuid4()) if model_scope is not None else None
            model_removal_fence = str(uuid.uuid4()) if model_scope is not None else None
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
                if model_operation_id is None or model_removal_fence is None:
                    raise AssertionError("model removal owner identity is missing")
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
                selector,
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
                first_blocker = current_blockers[0]
                raise RecipeImageAvailabilityRefused(
                    first_blocker.code,
                    first_blocker.detail,
                    retryable=first_blocker.retryable,
                    recovery_actions=first_blocker.recovery_actions,
                )

            model_children: list[RecipeCacheRemovalModelChild] = []
            if model_scope is not None:
                assert model_operation_id is not None
                assert model_removal_fence is not None
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
                    raise RecipeImageAvailabilityRefused(
                        ModelCacheCode.REMOVAL_SCOPE_CHANGED,
                        "model cache scope changed before the recipe removal was accepted",
                    )
                child_id, accepted_key, selected_sets, plan_digest = accepted_child
                if (
                    child_id != model_operation_id
                    or accepted_key != child_request_key
                    or selected_sets != model_scope.selected_sets
                ):
                    raise RecipeImageAvailabilityRefused(
                        ModelCacheCode.REMOVAL_SCOPE_CHANGED,
                        "accepted model removal does not match the reviewed recipe scope",
                    )
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
                action="remove",
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
            checkpoint = RecipeCacheRemovalCheckpoint(
                schema_version=SCHEMA_VERSION,
                image_index=0,
                image_pending_bytes=None,
                image_reclaimed_bytes=0,
                model_index=0,
                model_reclaimed_bytes=0,
                retry_attempts=0,
                failure=None,
            )
            owner = RecipeCacheRemovalOwner(
                schema_version=SCHEMA_VERSION,
                plan=plan,
                checkpoint=checkpoint,
            )
            owner_bytes = len(canonical_message(owner.model_dump(mode="json")))
            if owner_bytes > MAX_ARTIFACT_OWNER_SCAN_BYTES:
                raise RecipeImageAvailabilityInvalid(
                    RecipeImageCode.REMOVAL_SCOPE_LIMITED,
                    "recipe removal owner is "
                    f"{owner_bytes} bytes; limit is {MAX_ARTIFACT_OWNER_SCAN_BYTES} bytes",
                    reason=InvalidRequestReason.LIMIT_EXCEEDED,
                )
            now = self._clock()
            now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
            operation = self._lifecycle.new_job(
                id=operation_id,
                request_id=request_id,
                kind=REMOVE_OPERATION_KIND,
                actor=actor,
                authority_revision=revision_id,
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
    except IntegrityError:
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
    except ArtifactLifecycleError as error:
        raise RecipeImageAvailabilityRefused(
            error.code,
            error.detail,
            retryable=error.retryable,
            recovery_actions=("retry",) if error.retryable else (),
        ) from error
    except ModelCacheConflict as error:
        raise RecipeImageAvailabilityRefused(
            error.code,
            error.detail,
            retryable=error.recovery == "retry",
            retry_after_seconds=error.retry_after_seconds,
            recovery_actions=("retry",) if error.recovery == "retry" else (),
        ) from error

    with self._sessions() as session:
        operation = session.get(Job, operation_id)
        if operation is None:
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.OPERATION_MISSING,
                "accepted recipe removal owner could not be read",
                retryable=True,
                recovery_actions=("retry",),
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        intent = self._read_removal_intent(operation)
        return self._read_removal_result(operation, intent)


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
