"""Failure projection for exact recipe image availability."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from vonk_agent_protocol import (
    LifecycleState,
    OperationMemberProgress,
    OperationProgress,
    RecipeImageCode,
    RuntimeImageCode,
    SecurityRefusalError,
    UnknownOutcomeError,
    canonical_message,
)

from .. import job_states
from ..failure_classification import is_redownload
from ..job_documents import (
    AvailabilityJobResult,
    AvailabilityUnknownEnd,
)
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..lifecycle.image_availability import PREPARATION_BUDGET
from ..lifecycle.types import State
from ..model_cache import ModelCacheError
from ..models import (
    Job,
)
from ..operation_blockers import (
    make_blocker,
)
from ..operation_contract import (
    AvailabilityOperationFailure,
    sanitize_failure_evidence,
)
from ..operation_progress import aggregate_progress
from ..recipe_image_availability_reader_contract import (
    StoredAvailabilityIdentity,
)
from ..recipe_image_availability_view_contract import (
    RecipeImageAvailabilityView,
)
from ..stored_json import Residue, read_row_column
from ..strict_json import serialize_json_value
from .contracts import (
    _INTEGRITY_FAILURE_CODES,
    _LOGGER,
    DATABASE_BUSY_CODE,
    OPERATION_KIND,
    BuildUnsettled,
    RecipeImageAvailabilityClaim,
    _failure_code,
    _failure_detail,
    _iso,
    _log_excerpt,
    _read,
    _recovery_actions,
    _retry_after,
    _retryable,
)

_OBSERVATION_BUDGET = PREPARATION_BUDGET


if TYPE_CHECKING:
    from .service import RecipeImageAvailabilityService


def note_waiting_for_worker(self: RecipeImageAvailabilityService, busy: int) -> None:
    """Say why queued preparations are not running: every worker is busy."""

    with self._sessions.begin() as session:
        for operation in session.scalars(
            select(Job)
            .where(
                Job.kind == OPERATION_KIND,
                Job.state == "queued",
                Job.current_attempt == 0,
            )
            .order_by(Job.created_at, Job.id)
            .limit(16)
            .with_for_update(skip_locked=True)
        ):
            payload = self._payload(operation)
            if isinstance(payload, Residue) or payload.blockers:
                continue
            payload = self._record_blockers(
                operation,
                payload,
                [
                    make_blocker(
                        RecipeImageCode.WAITING_FOR_WORKER,
                        f"All {busy} image preparation workers are busy preparing "
                        "other runtime images; this starts when one is free.",
                        severity="info",
                    )
                ],
            )
            operation.payload = serialize_json_value(payload)


def _fail(
    self: RecipeImageAvailabilityService,
    claim: RecipeImageAvailabilityClaim,
    error: BaseException | BuildUnsettled,
) -> None:
    retryable = _retryable(error)
    code = _failure_code(error)
    detail = _failure_detail(error)
    step = getattr(error, "step", None)
    retry_after = _retry_after(error)
    preserved_retry_time = getattr(error, "retry_time", None)
    if isinstance(step, str) and step.strip():
        detail = f"{step.strip()}: {detail}"
    excerpt = _log_excerpt(error)
    with self._sessions.begin() as session:
        operation = self._claim_operation(session, claim)
        if operation is None:
            _LOGGER.warning(
                "recipe image preparation %s failed after its claim was lost "
                "(%s); its next claim will retry",
                claim.operation_id,
                _failure_code(error),
            )
            return
        payload = self._payload(operation)
        if isinstance(payload, Residue):
            self._lifecycle.fail(
                operation, self._clock(), reason=payload.reason.value, retryable=False
            )
            operation.payload = serialize_json_value(
                AvailabilityUnknownEnd(residue=payload.reason)
            )
            return
        retry = payload.retry
        automatic_attempts = retry.automatic_attempts
        bounded = retryable
        dependency_wait = (
            isinstance(error, BuildUnsettled)
            and error.settled_build_operation_id is None
            and retryable
        )
        retry = retry.model_copy(
            update={"automatic_attempts": automatic_attempts + int(not dependency_wait)}
        )
        now = self._clock()
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        created = operation.created_at
        created = created if created.tzinfo else created.replace(tzinfo=UTC)
        deadline = created + _OBSERVATION_BUDGET
        if now >= deadline and operation.current_attempt > 0:
            from .scheduling import _expire_preparation

            _expire_preparation(self, session, operation, payload, now)
            return
        if retryable:
            # Derived image evidence has a request-owned observation deadline.
            # Restart preserves it; a fresh request receives its own budget.
            retryable = retryable and now < deadline
        # The core decides the retry (rule 1) on its one bounded, jittered
        # clock; the error's own delay (``Retry-After``) is only the floor.
        floor = (
            now + timedelta(seconds=retry_after) if retry_after is not None else None
        )
        if isinstance(preserved_retry_time, str):
            try:
                preserved = datetime.fromisoformat(preserved_retry_time)
            except ValueError:
                preserved = None
            if preserved is not None:
                preserved = (
                    preserved
                    if preserved.tzinfo is not None
                    else preserved.replace(tzinfo=UTC)
                )
                floor = preserved if floor is None else max(floor, preserved)
        decided = self._lifecycle.plan_failure(
            operation,
            now,
            retryable=retryable,
            retry_after=floor,
            # Row contention is short-lived even after many dependency waits.
            # Keep the core's stable jitter, without inheriting outage backoff.
            count=0 if code == DATABASE_BUSY_CODE else automatic_attempts,
        )
        if decided.state is State.BACKOFF and decided.next_action_at is not None:
            next_observation = min(decided.next_action_at, deadline)
            decided = replace(decided, next_action_at=next_observation)
            retry_after = max(0, int((next_observation - now).total_seconds() + 0.999))
            preserved_retry_time = _iso(next_observation)
        # A definite end keeps whatever the error itself stated.
        required_bytes = getattr(error, "required_bytes", None)
        free_bytes = getattr(error, "free_bytes", None)
        shortfall_bytes = getattr(error, "shortfall_bytes", None)
        if required_bytes is None:
            required_bytes = getattr(error, "disk_required_bytes", None)
        if free_bytes is None:
            free_bytes = getattr(error, "disk_free_bytes", None)
        if (
            shortfall_bytes is None
            and type(required_bytes) is int
            and type(free_bytes) is int
        ):
            shortfall_bytes = max(0, required_bytes - free_bytes)
        failure = AvailabilityOperationFailure.model_validate(
            sanitize_failure_evidence(
                {
                    "code": str(code)[:64],
                    "detail": str(detail)[:512],
                    "recovery_actions": list(getattr(error, "recovery_actions", ()))
                    or _recovery_actions(str(code), retryable),
                    "retryable": retryable,
                    "retry_time": preserved_retry_time
                    if isinstance(preserved_retry_time, str)
                    else (
                        _iso(now + timedelta(seconds=retry_after))
                        if retry_after is not None
                        else None
                    ),
                    "retry_after_seconds": retry_after,
                    "log_excerpt": excerpt,
                    "required_bytes": required_bytes
                    if type(required_bytes) is int and required_bytes >= 0
                    else None,
                    "free_bytes": free_bytes
                    if type(free_bytes) is int and free_bytes >= 0
                    else None,
                    "shortfall_bytes": shortfall_bytes
                    if type(shortfall_bytes) is int and shortfall_bytes >= 0
                    else None,
                }
            )
        )
        operation.result = None
        payload = self._payload(operation)
        if isinstance(payload, Residue):
            self._lifecycle.fail(
                operation, self._clock(), reason=payload.reason.value, retryable=False
            )
            operation.payload = serialize_json_value(
                AvailabilityUnknownEnd(residue=payload.reason)
            )
            return
        # This exact live claim owns failure settlement. A leftover reference
        # from an obsolete attempt cannot keep its lease or veto retry.
        payload = payload.model_copy(update={"image_reference_intent": None})
        payload = payload.model_copy(update={"retry": retry, "failure": failure})
        blockers = list(getattr(error, "blockers", ())) or [
            make_blocker(
                str(code),
                str(detail),
                severity="warning" if bounded else "error",
            )
        ]
        payload = self._record_blockers(operation, payload, blockers)
        if bounded and (
            str(code) in _INTEGRITY_FAILURE_CODES or is_redownload(str(code))
        ):
            # Never reuse bytes that failed verification: the automatic
            # retry downloads or builds them again.
            payload = payload.model_copy(update={"force_rebuild": True})
        dependency = payload.build_dependency
        settled_build_id = getattr(error, "settled_build_operation_id", None)
        exact_build_settled = (
            isinstance(settled_build_id, str)
            and dependency is not None
            and str(dependency.operation_id) == settled_build_id
        )
        if exact_build_settled or str(code) == RuntimeImageCode.CACHE_MISSING:
            # The failed effect is settled; a later execution claim may
            # create a new child. Observation waits retain the exact child.
            payload = payload.model_copy(update={"build_dependency": None})
        payload = payload.model_copy(update={"retry_after_at": None})
        payload = payload.model_copy(update={"claim_owner": None})
        payload = payload.model_copy(update={"claim_until": None})
        updated = self._lifecycle.commit(
            operation, decided, now, reason=str(detail), payload=payload
        )
        operation.payload = serialize_json_value(
            updated if updated is not None else payload
        )


def _unknown_view(
    self: RecipeImageAvailabilityService, operation: Job, residue: Residue
) -> RecipeImageAvailabilityView:
    identity = _read(
        StoredAvailabilityIdentity, operation.payload, subject=operation.id
    )
    return RecipeImageAvailabilityView(
        id=operation.id,
        request_id=operation.request_id,
        request=identity.request if identity else None,
        kind=operation.kind,
        state=operation.state,
        attempt=int(operation.current_attempt or 0),
        recipe_revision_id=identity.recipe_revision_id
        if identity
        else operation.authority_revision,
        recipe_content_sha256=identity.recipe_content_sha256 if identity else None,
        model_digest=None,
        build_input_sha256=None,
        progress=identity.progress if identity else None,
        image_progress=None,
        result=None,
        failure=None,
        supported_actions=(),
        created_at=_iso(operation.created_at),
        updated_at=_iso(operation.updated_at),
        cancellation=self._stored_cancellation(operation),
        residue=residue,
    )


def _view(
    self: RecipeImageAvailabilityService, operation: Job
) -> RecipeImageAvailabilityView:
    payload = self._payload(operation)
    if isinstance(payload, Residue):
        return self._unknown_view(operation, payload)
    stored_result = read_row_column(operation, "result")
    result = stored_result if isinstance(stored_result, AvailabilityJobResult) else None
    observation_residue = None
    try:
        model_child = self._current_model_child(payload)
    except (
        SecurityRefusalError,
        UnknownOutcomeError,
        ModelCacheError,
        SQLAlchemyError,
        OSError,
        ValueError,
    ) as error:
        model_child = payload.model_child
        observation_residue = retire_as_unknown(
            "recipe-image.model-child",
            operation.id,
            BookkeepingReason.EVIDENCE_UNAVAILABLE,
            _failure_detail(error),
        )
    image_progress = payload.progress
    image_result = payload.image_result
    image_ready = image_result is not None
    image_state = "succeeded" if image_ready else operation.state
    failure = payload.failure
    state = operation.state
    residue = observation_residue
    if (
        state == "succeeded"
        and (result is None or failure is not None)
        or state == "failed"
        and failure is None
    ):
        # Missing terminal evidence is typed unknown, not confirmed success
        # or an assertion that this terminal row is executing again.
        residue = retire_as_unknown(
            "recipe-image.result", operation.id, BookkeepingReason.EVIDENCE_MISMATCH
        )
        result = None
    if state != "succeeded":
        result = None
    if image_result is not None:
        image_progress = image_progress.model_copy(
            update={
                "phase": "available",
                "completed_bytes": image_result.image_bytes,
                "total_bytes": image_result.image_bytes,
                "total_bytes_known": True,
                "bytes_per_second": None,
                "eta_seconds": None,
            }
        )
    shared = (
        OperationProgress.model_fields.keys()
        & OperationMemberProgress.model_fields.keys()
    )
    members = [
        OperationMemberProgress.model_validate_json(
            canonical_message(
                image_progress.model_dump(mode="json", include=shared)
                | {"member_id": "runtime-image", "state": image_state}
            )
        )
    ]
    if model_child is not None and model_child.progress is not None:
        members.append(
            OperationMemberProgress.model_validate_json(
                canonical_message(
                    model_child.progress.model_dump(mode="json", include=shared)
                    | {"member_id": "model-cache", "state": model_child.state}
                )
            )
        )
    progress = (
        aggregate_progress(members)
        if len(members) > 1
        else image_progress.model_copy(update={"members": members})
    )
    next_attempt_at = (
        _iso(payload.retry_after_at)
        if payload.retry_after_at is not None
        and state in job_states.words(LifecycleState.QUEUED, LifecycleState.BACKOFF)
        else None
    )
    return RecipeImageAvailabilityView(
        id=operation.id,
        request_id=operation.request_id,
        request=payload.request,
        kind=operation.kind,
        state=state,
        attempt=int(operation.current_attempt),
        recipe_revision_id=payload.recipe_revision_id,
        recipe_content_sha256=payload.recipe_content_sha256,
        model_digest=payload.model_digest,
        build_input_sha256=payload.build_input_sha256,
        progress=progress,
        image_progress=image_progress,
        image_state=image_state,
        image_failure=None if image_ready else failure,
        result=result,
        failure=failure,
        supported_actions=tuple(action.value for action in failure.recovery_actions)
        if failure
        else (),
        created_at=_iso(operation.created_at),
        updated_at=_iso(operation.updated_at),
        model_child=model_child,
        cancellation=self._stored_cancellation(operation),
        blockers=tuple(payload.blockers or [])
        if state
        in job_states.words(
            LifecycleState.QUEUED, LifecycleState.BACKOFF, LifecycleState.FAILED
        )
        else (),
        next_attempt_at=next_attempt_at,
        residue=residue,
    )
