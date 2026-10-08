"""Public preparation service, delegating focused concerns to package modules."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    LifecycleState,
    OperationProgress,
)
from vonk_forge_contracts import RecipeDefinition

from .. import job_states
from ..artifact_lifecycle import (
    ArtifactIdentity,
    RemovalOwnerKind,
)
from ..artifact_reference_scan import (
    ArtifactReferenceFinding,
)
from ..cache_removal_review import (
    CacheRemovalAsset,
    CacheRemovalBlocker,
    CacheRemovalFinding,
    CacheRemovalReview,
)
from ..categorized_errors import InvalidValue
from ..install_admission import InstallPlan
from ..job_documents import (
    AvailabilityJobPayload,
    AvailabilityModelChild,
    AvailabilityRuntime,
)
from ..lifecycle.image_availability import ImageAvailabilityAdapter
from ..model_cache import (
    ModelCacheRemovalScope,
)
from ..models import (
    CatalogDocumentRevision,
    Job,
    ModelCacheOperation,
    RecipeBuild,
)
from ..operation_blockers import (
    OperationBlocker,
)
from ..recipe_availability_intent import (
    RecipeAvailabilityIntent,
    RecipeRevisionIntent,
    RecipeSelectorIntent,
)
from ..recipe_image_availability_reader_contract import (
    StoredAvailabilityIdentity,
)
from ..recipe_image_availability_view_contract import (
    RecipeCacheRemovalStatus,
    RecipeImageAvailabilityView,
)
from ..recipe_image_removal_contract import (
    RecipeCacheRemovalIntent,
    RecipeCacheRemovalModelChild,
    RecipeCacheRemovalOwner,
    RecipeCacheRemovalPlan,
    RecipeCacheRemovalResult,
    RecipeRemovalUnavailableView,
)
from ..recipe_lifecycle_contract import RecipeOperationCancellationResult
from ..recipe_update_contract import RecipeUpdateResponse
from ..runtime_image_preparation import (
    OCIImageTransport,
    RuntimeImageReceipt,
    RuntimeImageReferenceIntent,
)
from ..stored_json import Residue
from ..worker_memory_contract import WorkerMemoryComponent
from . import (
    cancellation as cancellation_ops,
)
from . import (
    cancellation_reconcile,
    execution,
    failure_projection,
    image_preparation,
    model_preparation,
    persistence,
    projection,
    removal_acceptance,
    removal_completion,
    removal_execution,
    removal_review,
    requests,
    scheduling,
)
from .contracts import (
    BuildUnsettled,
    ModelCacheOperationHandle,
    RecipeImageAvailabilityClaim,
    RuntimeImageCacheStorage,
    _RecipeRemovalSelection,
)

if TYPE_CHECKING:
    from ..recipe_update_batches import RecipeUpdateClaim


class RecipeImageAvailabilityService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        storage: RuntimeImageCacheStorage,
        authority: Callable[..., tuple[RecipeDefinition, object]],
        transport: OCIImageTransport | None = None,
        builder: Callable[..., object] | None = None,
        clock: Callable[[], datetime],
        model_cache: Any | None = None,
        max_parallel: int = 4,
        builder_admission: Callable[..., None] | None = None,
        claim_lease_seconds: int = 120,
    ) -> None:
        if not 1 <= max_parallel <= 16:
            raise InvalidValue("availability parallelism is invalid")
        if not 10 <= claim_lease_seconds <= 3_600:
            raise InvalidValue("availability claim lease is invalid")
        self._sessions = sessions
        self._storage = storage
        self._authority = authority
        self._transport = transport
        self._builder = builder
        self._clock = clock
        self._removal_gate_after: tuple[str, str] | None = None
        self._removal_request_after: str | None = None
        self._lifecycle = ImageAvailabilityAdapter(clock=clock)
        self._model_cache = model_cache
        self._max_parallel = max_parallel
        self._builder_admission = builder_admission
        self._claim_lease_seconds = claim_lease_seconds
        self._identity_locks: dict[str, threading.Lock] = {}
        self._identity_locks_guard = threading.Lock()
        self._removal_lock = threading.RLock()
        from ..recipe_update_batches import RecipeUpdateBatches

        self._updates = RecipeUpdateBatches(self, sessions)

    def prepare_install(self, plan: InstallPlan, actor: str, request_id: str) -> None:
        # The durable child owns observation/rebuild, including restart and its
        # total deadline. Installation keeps its reviewed exact image binding.
        with self._sessions() as session:
            build = session.get(RecipeBuild, plan.recipe_build_id)
            input_digest = build.build_input_sha256 if build is not None else None
        preparation = self.start(
            plan.recipe_revision_id,
            actor=actor,
            request_id=str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"vonk:install-preparation:{request_id}")
            ),
            build_input_sha256=input_digest,
        )
        # Dispatch only this request's child through the normal fenced owner.
        # No acceptance transaction is held while it verifies/reconstructs.
        for claim in self.claim_pending(limit=1, operation_id=preparation.id):
            self.run_claim(claim)

    def _payload(self, operation: Job) -> AvailabilityJobPayload | Residue:
        return persistence._payload(self, operation)

    def _recipe_removal_selection_in_session(
        self, session: Session, selector: str, *, with_model: bool
    ) -> _RecipeRemovalSelection:
        return persistence._recipe_removal_selection_in_session(
            self, session, selector, with_model=with_model
        )

    def _resolve_recipe_selector(self, selector: str) -> str:
        return persistence._resolve_recipe_selector(self, selector)

    @staticmethod
    def _resolve_recipe_selector_in_session(session: Session, selector: str) -> str:
        return persistence._resolve_recipe_selector_in_session(session, selector)

    def start_selector(
        self, selector: str, *, actor: str, request_id: str, force: bool = False
    ) -> RecipeImageAvailabilityView:
        return persistence.start_selector(
            self, selector, actor=actor, request_id=request_id, force=force
        )

    @staticmethod
    def _removal_payload_digest(payload: RecipeCacheRemovalPlan) -> str:
        return persistence._removal_payload_digest(payload)

    def _read_removal_owner(self, operation: Job) -> RecipeCacheRemovalOwner:
        return persistence._read_removal_owner(self, operation)

    def _read_removal_intent(self, operation: Job) -> RecipeCacheRemovalIntent:
        return persistence._read_removal_intent(self, operation)

    @staticmethod
    def _removal_progress_document(
        operation: Job, owner: RecipeCacheRemovalOwner
    ) -> OperationProgress:
        return persistence._removal_progress_document(operation, owner)

    def _read_removal_result(
        self, operation: Job, intent: RecipeCacheRemovalIntent
    ) -> RecipeCacheRemovalStatus:
        return persistence._read_removal_result(self, operation, intent)

    @staticmethod
    def _stored_removal_result(
        operation: Job, intent: RecipeCacheRemovalIntent, owner: RecipeCacheRemovalOwner
    ) -> RecipeCacheRemovalResult | None:
        return persistence._stored_removal_result(operation, intent, owner)

    def _replay_removal(
        self,
        operation: Job,
        *,
        selector: str,
        actor: str,
        request_id: str,
        with_model: bool,
    ) -> RecipeCacheRemovalStatus:
        return persistence._replay_removal(
            self,
            operation,
            selector=selector,
            actor=actor,
            request_id=request_id,
            with_model=with_model,
        )

    @staticmethod
    def _cache_removal_finding(
        finding: ArtifactReferenceFinding,
    ) -> CacheRemovalFinding:
        return removal_review._cache_removal_finding(finding)

    def _recipe_removal_impact_in_session(
        self,
        session: Session,
        selector: str,
        *,
        with_model: bool,
        own_assignments: Sequence[
            tuple[ArtifactIdentity, RemovalOwnerKind, str, str]
        ] = (),
    ) -> tuple[
        _RecipeRemovalSelection,
        tuple[CacheRemovalFinding, ...],
        tuple[CacheRemovalFinding, ...],
        tuple[CacheRemovalBlocker, ...],
    ]:
        return removal_review._recipe_removal_impact_in_session(
            self,
            session,
            selector,
            with_model=with_model,
            own_assignments=own_assignments,
        )

    def _runtime_image_removal_assets(
        self, selection: _RecipeRemovalSelection
    ) -> tuple[tuple[CacheRemovalAsset, ...], tuple[CacheRemovalBlocker, ...]]:
        return removal_review._runtime_image_removal_assets(self, selection)

    def _model_removal_assets(
        self, scope: ModelCacheRemovalScope | None
    ) -> tuple[CacheRemovalAsset, ...]:
        return removal_review._model_removal_assets(self, scope)

    @staticmethod
    def _review_assets_for_selection(
        selection: _RecipeRemovalSelection, observed_assets: Sequence[CacheRemovalAsset]
    ) -> tuple[CacheRemovalAsset, ...]:
        return removal_review._review_assets_for_selection(selection, observed_assets)

    def _sealed_recipe_removal_review(
        self,
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
        return removal_review._sealed_recipe_removal_review(
            self,
            selector=selector,
            with_model=with_model,
            selection=selection,
            references=references,
            active_work=active_work,
            blockers=blockers,
            assets=assets,
            now=now,
        )

    def review_removal(self, selector: str, *, with_model: bool) -> CacheRemovalReview:
        return removal_review.review_removal(self, selector, with_model=with_model)

    def remove_selector(
        self, selector: str, *, actor: str, request_id: str, with_model: bool = False
    ) -> RecipeCacheRemovalStatus:
        return removal_acceptance.remove_selector(
            self, selector, actor=actor, request_id=request_id, with_model=with_model
        )

    def reconcile_requested_removals(self, *, limit: int = 64) -> int:
        return removal_acceptance.reconcile_requested_removals(self, limit=limit)

    def reconcile_removal_gates(self, *, limit: int = 64) -> int:
        return removal_execution.reconcile_removal_gates(self, limit=limit)

    def advance_removals(self, *, limit: int = 1) -> int:
        return removal_execution.advance_removals(self, limit=limit)

    def _advance_recipe_removal(self, operation_id: str) -> bool:
        return removal_execution._advance_recipe_removal(self, operation_id)

    def _advance_recipe_image_removal(
        self,
        operation_id: str,
        observed_owner: RecipeCacheRemovalOwner,
        archive_sha256: str,
        *,
        now: datetime,
    ) -> bool:
        return removal_execution._advance_recipe_image_removal(
            self, operation_id, observed_owner, archive_sha256, now=now
        )

    def _checkpoint_recipe_image_removal(
        self,
        operation_id: str,
        observed_owner: RecipeCacheRemovalOwner,
        *,
        identity: ArtifactIdentity,
        observed_bytes: int,
        now: datetime,
    ) -> int | None:
        return removal_execution._checkpoint_recipe_image_removal(
            self,
            operation_id,
            observed_owner,
            identity=identity,
            observed_bytes=observed_bytes,
            now=now,
        )

    def _complete_recipe_image_removal(
        self,
        operation_id: str,
        observed_owner: RecipeCacheRemovalOwner,
        *,
        identity: ArtifactIdentity,
        pending_bytes: int,
        now: datetime,
    ) -> bool:
        return removal_execution._complete_recipe_image_removal(
            self,
            operation_id,
            observed_owner,
            identity=identity,
            pending_bytes=pending_bytes,
            now=now,
        )

    def _advance_recipe_model_child(
        self,
        operation_id: str,
        owner: RecipeCacheRemovalOwner,
        child: RecipeCacheRemovalModelChild,
        *,
        now: datetime,
    ) -> bool:
        return removal_execution._advance_recipe_model_child(
            self, operation_id, owner, child, now=now
        )

    def _complete_recipe_model_child(
        self,
        operation_id: str,
        observed_owner: RecipeCacheRemovalOwner,
        child: RecipeCacheRemovalModelChild,
        *,
        reclaimed_bytes: int,
        now: datetime,
    ) -> bool:
        return removal_execution._complete_recipe_model_child(
            self,
            operation_id,
            observed_owner,
            child,
            reclaimed_bytes=reclaimed_bytes,
            now=now,
        )

    def _record_recipe_removal_failure(
        self,
        operation_id: str,
        *,
        code: str,
        detail: str,
        retryable: bool,
        retry_after_seconds: int = 5,
    ) -> bool:
        return removal_execution._record_recipe_removal_failure(
            self,
            operation_id,
            code=code,
            detail=detail,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
        )

    def _finish_recipe_removal(
        self,
        operation_id: str,
        observed_owner: RecipeCacheRemovalOwner,
        *,
        now: datetime,
    ) -> bool:
        return removal_completion._finish_recipe_removal(
            self, operation_id, observed_owner, now=now
        )

    def update(
        self, *, actor: str, request_id: str, selectors: list[str] | None, all: bool
    ) -> RecipeUpdateResponse:
        return cancellation_ops.update(
            self, actor=actor, request_id=request_id, selectors=selectors, all=all
        )

    def cancel(
        self, operation_id: str, *, actor: str, request_id: str, reason: str
    ) -> RecipeImageAvailabilityView | RecipeUpdateResponse:
        return cancellation_ops.cancel(
            self, operation_id, actor=actor, request_id=request_id, reason=reason
        )

    def _request_cancellation(
        self,
        session: Session,
        job: Job,
        *,
        actor: str,
        request_id: str,
        reason: str,
        authorize: bool,
    ) -> RecipeOperationCancellationResult | Residue:
        return cancellation_ops._request_cancellation(
            self,
            session,
            job,
            actor=actor,
            request_id=request_id,
            reason=reason,
            authorize=authorize,
        )

    def _stored_cancellation(
        self, job: Job
    ) -> RecipeOperationCancellationResult | None:
        return cancellation_ops._stored_cancellation(self, job)

    def _cancel_update_child(
        self, operation_id: str, cancellation: RecipeOperationCancellationResult
    ) -> RecipeImageAvailabilityView | None:
        return cancellation_ops._cancel_update_child(self, operation_id, cancellation)

    def reconcile_cancellations(self, *, limit: int = 8) -> int:
        return cancellation_ops.reconcile_cancellations(self, limit=limit)

    def _model_child_cancellation_pending(
        self,
        operation_id: str,
        payload: AvailabilityJobPayload,
        request_id: str,
        cancellation: RecipeOperationCancellationResult,
    ) -> tuple[bool, bool]:
        return cancellation_ops._model_child_cancellation_pending(
            self, operation_id, payload, request_id, cancellation
        )

    def _build_child_cancellation_pending(
        self,
        operation_id: str,
        payload: AvailabilityJobPayload,
        current_attempt: int,
        cancellation: RecipeOperationCancellationResult,
    ) -> bool:
        return cancellation_ops._build_child_cancellation_pending(
            self, operation_id, payload, current_attempt, cancellation
        )

    def _release_cancelled_claim(self, claim: RecipeImageAvailabilityClaim) -> bool:
        return cancellation_reconcile._release_cancelled_claim(self, claim)

    def _reconcile_availability_cancellation(self, operation_id: str) -> bool:
        return cancellation_reconcile._reconcile_availability_cancellation(
            self, operation_id
        )

    def _end_spent_cancellation(self, operation_id: str) -> bool:
        return cancellation_reconcile._end_spent_cancellation(self, operation_id)

    def _reconcile_cancellation_pass(self, operation_id: str) -> bool:
        return cancellation_reconcile._reconcile_cancellation_pass(self, operation_id)

    def claim_update(self, owner: str) -> RecipeUpdateClaim | None:
        return cancellation_reconcile.claim_update(self, owner)

    def run_update_claim(self, claim: RecipeUpdateClaim) -> None:
        return cancellation_reconcile.run_update_claim(self, claim)

    def update_activity_provider(self):
        return cancellation_reconcile.update_activity_provider(self)

    def start(
        self,
        recipe_revision_id: str,
        *,
        actor: str,
        request_id: str,
        model_digest: str | None = None,
        build_input_sha256: str | None = None,
        effective_execution_key: str | None = None,
        force: bool = False,
        force_rebuild: bool = False,
    ) -> RecipeImageAvailabilityView:
        return requests.start(
            self,
            recipe_revision_id,
            actor=actor,
            request_id=request_id,
            model_digest=model_digest,
            build_input_sha256=build_input_sha256,
            effective_execution_key=effective_execution_key,
            force=force,
            force_rebuild=force_rebuild,
        )

    def cancel_profile_preparation(
        self,
        recipe_revision_id: str,
        *,
        actor: str,
        reason: str,
        application_id: str | None = None,
    ) -> tuple[str, ...]:
        return requests.cancel_profile_preparation(
            self,
            recipe_revision_id,
            actor=actor,
            reason=reason,
            application_id=application_id,
        )

    def ensure_preparation(
        self, recipe_revision_id: str, *, actor: str, application_id: str | None = None
    ) -> tuple[OperationBlocker, ...]:
        return requests.ensure_preparation(
            self, recipe_revision_id, actor=actor, application_id=application_id
        )

    def _refresh_authority(
        self, recipe_revision_id: str, *, force: bool
    ) -> tuple[RecipeDefinition, AvailabilityRuntime | None]:
        return requests._refresh_authority(self, recipe_revision_id, force=force)

    def _start_request(
        self,
        intent: RecipeSelectorIntent | RecipeRevisionIntent,
        *,
        actor: str,
        request_id: str,
        update_claim: RecipeUpdateClaim | None = None,
    ) -> RecipeImageAvailabilityView:
        return requests._start_request(
            self, intent, actor=actor, request_id=request_id, update_claim=update_claim
        )

    @staticmethod
    def _lock_build_consumer(session: Session, payload: AvailabilityJobPayload) -> None:
        return requests._lock_build_consumer(session, payload)

    def _model_progress(self, value: object) -> OperationProgress:
        return model_preparation._model_progress(self, value)

    def _refresh_model_observation(
        self, child: AvailabilityModelChild, operation: ModelCacheOperationHandle
    ) -> AvailabilityModelChild:
        return model_preparation._refresh_model_observation(self, child, operation)

    def _ensure_model_child(
        self, recipe_revision_id: str, *, actor: str, parent_request_key: str
    ) -> AvailabilityModelChild | None:
        return model_preparation._ensure_model_child(
            self, recipe_revision_id, actor=actor, parent_request_key=parent_request_key
        )

    def _start_model_repair(
        self, operation: object, *, actor: str, parent_request_key: str
    ) -> ModelCacheOperationHandle:
        return model_preparation._start_model_repair(
            self, operation, actor=actor, parent_request_key=parent_request_key
        )

    def _resume_model_child(
        self,
        child: AvailabilityModelChild | None,
        *,
        actor: str,
        parent_request_key: str,
    ) -> AvailabilityModelChild | None:
        return model_preparation._resume_model_child(
            self, child, actor=actor, parent_request_key=parent_request_key
        )

    def _matching_request(
        self, existing: Job, *, actor: str, intent: RecipeAvailabilityIntent
    ) -> RecipeImageAvailabilityView:
        return projection._matching_request(self, existing, actor=actor, intent=intent)

    def _request_replay(
        self, request_id: str, *, actor: str, intent: RecipeAvailabilityIntent
    ) -> RecipeImageAvailabilityView | None:
        return projection._request_replay(self, request_id, actor=actor, intent=intent)

    def get(self, operation_id: str) -> RecipeImageAvailabilityView:
        return projection.get(self, operation_id)

    def get_operator_operation(
        self, operation_id: str
    ) -> (
        RecipeImageAvailabilityView
        | RecipeUpdateResponse
        | RecipeRemovalUnavailableView
        | RecipeCacheRemovalStatus
    ):
        return projection.get_operator_operation(self, operation_id)

    def get_operator_request(
        self, request_key: str, *, actor: str
    ) -> (
        RecipeImageAvailabilityView
        | RecipeUpdateResponse
        | RecipeRemovalUnavailableView
        | RecipeCacheRemovalStatus
    ):
        return projection.get_operator_request(self, request_key, actor=actor)

    def list_page(
        self,
        *,
        recipe_revision_id: str | None = None,
        state: str | None = None,
        limit: int = 50,
        boundary: tuple[str, str] | None = None,
    ) -> tuple[tuple[RecipeImageAvailabilityView, ...], int, tuple[str, str] | None]:
        return projection.list_page(
            self,
            recipe_revision_id=recipe_revision_id,
            state=state,
            limit=limit,
            boundary=boundary,
        )

    def retry(
        self, operation_id: str, *, actor: str, request_id: str
    ) -> RecipeImageAvailabilityView:
        return projection.retry(self, operation_id, actor=actor, request_id=request_id)

    def resume_operations(self, *, limit: int = 16) -> int:
        return scheduling.resume_operations(self, limit=limit)

    def run_pending(self, *, limit: int = 1) -> int:
        return scheduling.run_pending(self, limit=limit)

    def claim_pending(
        self,
        *,
        limit: int = 4,
        owner_id: str | None = None,
        operation_id: str | None = None,
    ) -> tuple[RecipeImageAvailabilityClaim, ...]:
        return scheduling.claim_pending(
            self, limit=limit, owner_id=owner_id, requested_operation_id=operation_id
        )

    def _park_for_model(
        self, operation: Job, payload: AvailabilityJobPayload, now: datetime
    ) -> bool:
        return scheduling._park_for_model(self, operation, payload, now)

    @staticmethod
    def _holds_live_lease(payload: AvailabilityJobPayload, now: datetime) -> bool:
        return scheduling._holds_live_lease(payload, now)

    def _cancel_superseded_operation(
        self, operation: Job, newer_revision_id: str, *, now: datetime
    ) -> bool:
        return scheduling._cancel_superseded_operation(
            self, operation, newer_revision_id, now=now
        )

    def _cancel_older_preparations(
        self,
        session: Session,
        *,
        newer_revision: CatalogDocumentRevision,
        now: datetime,
        limit: int = 64,
    ) -> tuple[str, ...]:
        return scheduling._cancel_older_preparations(
            self, session, newer_revision=newer_revision, now=now, limit=limit
        )

    def _cancel_superseded_by_active_head(
        self, session: Session, *, now: datetime, limit: int = 64
    ) -> tuple[str, ...]:
        return scheduling._cancel_superseded_by_active_head(
            self, session, now=now, limit=limit
        )

    def run_claim(self, claim: RecipeImageAvailabilityClaim) -> None:
        return execution.run_claim(self, claim)

    def memory_footprint(self) -> dict[WorkerMemoryComponent, int]:
        return execution.memory_footprint(self)

    def _identity_lock(self, identity_key: str | None) -> threading.Lock:
        return execution._identity_lock(self, identity_key)

    def _eligible(self, operation_id: str) -> bool:
        return execution._eligible(self, operation_id)

    @staticmethod
    def _retry_due(payload: AvailabilityJobPayload, now: datetime) -> bool:
        return execution._retry_due(payload, now)

    def _claim_operation(
        self,
        session: Session,
        claim: RecipeImageAvailabilityClaim,
        *,
        allowed_states: tuple[str, ...] = job_states.words(LifecycleState.RUNNING),
    ) -> Job | None:
        return execution._claim_operation(
            self, session, claim, allowed_states=allowed_states
        )

    def _require_claim(
        self,
        session: Session,
        claim: RecipeImageAvailabilityClaim,
        *,
        allowed_states: tuple[str, ...] = job_states.words(LifecycleState.RUNNING),
    ) -> Job:
        return execution._require_claim(
            self, session, claim, allowed_states=allowed_states
        )

    def _run(self, claim: RecipeImageAvailabilityClaim) -> None:
        return execution._run(self, claim)

    def _stored_recipe(
        self, payload: AvailabilityJobPayload | StoredAvailabilityIdentity | object
    ) -> RecipeDefinition | Residue:
        return execution._stored_recipe(self, payload)

    def _current_model_child(
        self,
        payload: AvailabilityJobPayload,
        *,
        actor: str | None = None,
        parent_request_key: str | None = None,
    ) -> AvailabilityModelChild | None:
        return execution._current_model_child(
            self, payload, actor=actor, parent_request_key=parent_request_key
        )

    def _update_model_progress(
        self, claim: RecipeImageAvailabilityClaim, child: AvailabilityModelChild
    ) -> bool:
        return execution._update_model_progress(self, claim, child)

    @staticmethod
    def _model_child_has_cancel_intent(operation: ModelCacheOperation) -> bool:
        return execution._model_child_has_cancel_intent(operation)

    def _defer_for_model(self, claim: RecipeImageAvailabilityClaim) -> None:
        return execution._defer_for_model(self, claim)

    def _prepare_claimed_image(
        self,
        claim: RecipeImageAvailabilityClaim,
        payload: AvailabilityJobPayload,
        recipe: RecipeDefinition,
        runtime: AvailabilityRuntime,
    ) -> RuntimeImageReceipt | BuildUnsettled:
        return image_preparation._prepare_claimed_image(
            self, claim, payload, recipe, runtime
        )

    def _renew_claim_loop(
        self, claim: RecipeImageAvailabilityClaim, stop: threading.Event
    ) -> None:
        return image_preparation._renew_claim_loop(self, claim, stop)

    def _renew_claim(self, claim: RecipeImageAvailabilityClaim) -> bool:
        return image_preparation._renew_claim(self, claim)

    def _persist_receipt(
        self, claim: RecipeImageAvailabilityClaim, receipt: RuntimeImageReceipt
    ) -> None:
        return image_preparation._persist_receipt(self, claim, receipt)

    def _persist_provisional_image_reference(
        self, claim: RecipeImageAvailabilityClaim, *, receipt: RuntimeImageReceipt
    ) -> None:
        return image_preparation._persist_provisional_image_reference(
            self, claim, receipt=receipt
        )

    @staticmethod
    def _image_reference_intent_for_claim(
        payload: AvailabilityJobPayload, claim: RecipeImageAvailabilityClaim
    ) -> RuntimeImageReferenceIntent | None:
        return image_preparation._image_reference_intent_for_claim(payload, claim)

    def _set_progress(
        self,
        operation: Job,
        phase: str,
        *,
        total_bytes: int | None = None,
        completed_bytes: int = 0,
        bytes_per_second: float | None = None,
        eta_seconds: float | None = None,
        detail: OperationProgress | None = None,
    ) -> None:
        return image_preparation._set_progress(
            self,
            operation,
            phase,
            total_bytes=total_bytes,
            completed_bytes=completed_bytes,
            bytes_per_second=bytes_per_second,
            eta_seconds=eta_seconds,
            detail=detail,
        )

    def _update_progress(
        self,
        claim: RecipeImageAvailabilityClaim,
        phase: str,
        *,
        total_bytes: int | None = None,
        completed_bytes: int = 0,
        detail: OperationProgress | None = None,
    ) -> None:
        return image_preparation._update_progress(
            self,
            claim,
            phase,
            total_bytes=total_bytes,
            completed_bytes=completed_bytes,
            detail=detail,
        )

    @staticmethod
    def _record_blockers(
        operation: Job,
        payload: AvailabilityJobPayload,
        blockers: Sequence[OperationBlocker],
    ) -> AvailabilityJobPayload:
        return image_preparation._record_blockers(operation, payload, blockers)

    def note_waiting_for_worker(self, busy: int) -> None:
        return failure_projection.note_waiting_for_worker(self, busy)

    def _fail(
        self, claim: RecipeImageAvailabilityClaim, error: BaseException | BuildUnsettled
    ) -> None:
        return failure_projection._fail(self, claim, error)

    def _unknown_view(
        self, operation: Job, residue: Residue
    ) -> RecipeImageAvailabilityView:
        return failure_projection._unknown_view(self, operation, residue)

    def _view(self, operation: Job) -> RecipeImageAvailabilityView:
        return failure_projection._view(self, operation)
