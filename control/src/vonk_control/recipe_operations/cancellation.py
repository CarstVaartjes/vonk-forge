"""Cancellation for digest-bound recipe operations."""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import or_, select
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    canonical_message,
)

from .. import job_states
from ..agent_jobs import (
    _JsonFlagIsTrue,
)
from ..categorized_errors import (
    InvalidValue,
    MissingRecord,
)
from ..lifecycle import Outcome
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..lifecycle.recipe_operation import RecipeOperationAdapter
from ..models import (
    AgentNode,
    AgentOperation,
    Job,
)
from ..recipe_build_cancellation import (
    BuildConsumerError,
    build_cancellation,
)
from ..recipe_lifecycle_contract import (
    RecipeOperationCancellationResult,
)
from ..recipe_progress import (
    _cancel_requested as _cancel_requested,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _parent_intent as _parent_intent,  # noqa: PLC0414 -- shared helper export
)
from ..stored_json import read_row_column
from .constants import _WORKLOAD_INTENT_KINDS
from .errors import RecipeOperationConflict, RecipeRequestInvalid
from .intent import _cancel_reason
from .interfaces import RecipeOperationView
from .observation_helpers import _aware
from .results import _recorded_result, _recorded_result_document, _validated_result

if TYPE_CHECKING:
    from .service import RecipeOperationService


class CancellationMixin:
    def get(self, operation_id: str) -> RecipeOperationView:
        service = typing_cast("RecipeOperationService", self)
        with service._sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or not job.kind.startswith("recipe."):
                raise MissingRecord(operation_id)
            return service._view(job, session=session)

    def cancel(
        self, operation_id: str, *, actor: str, request_id: str, reason: str
    ) -> RecipeOperationView:
        """Durably cancel a queued/running recipe operation."""
        service = typing_cast("RecipeOperationService", self)
        now = service._clock()
        cancellation_reason = _cancel_reason(reason)
        with service._sessions() as session:
            candidate = session.get(Job, operation_id)
            is_build = (
                candidate is not None
                and candidate.kind == WireAgentOperation.RECIPE_BUILD.value
            )
        if is_build:
            service._cancel_build(
                operation_id,
                actor=actor,
                request_id=request_id,
                reason=cancellation_reason,
            )
            return service.get(operation_id)
        with service._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None or not job.kind.startswith("recipe."):
                raise RecipeRequestInvalid("recipe operation is not cancellable")
            bound_ordinal = _parent_intent(job)
            superseded = False
            if (
                job.kind
                in _WORKLOAD_INTENT_KINDS | {WireAgentOperation.RECIPE_JOB_RUN.value}
                and type(bound_ordinal) is int
            ):
                nodes = tuple(
                    session.scalars(
                        select(AgentNode)
                        .where(AgentNode.node_id.in_(job.targets))
                        .order_by(AgentNode.node_id)
                    )
                )
                superseded = (
                    len(nodes) == len(job.targets)
                    and tuple(node.node_id for node in nodes)
                    == tuple(sorted(set(job.targets)))
                    and any(
                        node.workload_intent_ordinal > bound_ordinal for node in nodes
                    )
                )
            already_invalidated = superseded and (
                job.state
                in {LifecycleState.CANCELLED.value, LifecycleState.FAILED.value}
                or (
                    job.state in job_states.words(LifecycleState.NEEDS_OPERATOR)
                    and _cancel_requested(job)
                )
            )
            if already_invalidated:
                if (
                    not isinstance(actor, str)
                    or not 1 <= len(actor) <= 256
                    or not isinstance(request_id, str)
                ):
                    raise RecipeRequestInvalid(
                        "cancellation request identity is invalid"
                    )
                reused = session.scalar(
                    select(Job.id)
                    .where(
                        Job.id != job.id,
                        or_(
                            Job.request_id == request_id,
                            Job.result["cancel_request_id"].as_string() == request_id,
                        ),
                    )
                    .limit(1)
                )
                if reused is not None:
                    raise RecipeRequestInvalid(
                        "cancellation request key was already used differently",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                try:
                    if str(uuid.UUID(request_id)) != request_id:
                        raise InvalidValue("noncanonical cancellation request key")
                except ValueError as error:
                    raise RecipeRequestInvalid(
                        "cancellation request identity is invalid"
                    ) from error
                logging.getLogger(__name__).info(
                    "ignored cancellation of superseded recipe operation %s", job.id
                )
                return service._view(job)
            if job.state == LifecycleState.CANCELLED.value:
                previous = _recorded_result(
                    job.kind, read_row_column(job, "result"), subject=job.id
                )
                if isinstance(previous, RecipeOperationCancellationResult) and (
                    previous.cancel_request_id == request_id
                    and previous.reason == cancellation_reason
                    and previous.cancel_actor == actor
                ):
                    return service._view(job)
                raise RecipeRequestInvalid(
                    "cancellation request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
            if job.state not in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.NEEDS_OPERATOR,
            ):
                # A cancel always completes (rule 4): a parent that mirrors an
                # order's wait (the Stop of a one-shot job in doubt, a legacy
                # parked order) accepts it, and the orders end it.
                raise RecipeRequestInvalid("recipe operation is not cancellable")
            previous = _recorded_result(
                job.kind, read_row_column(job, "result"), subject=job.id
            )
            if isinstance(previous, RecipeOperationCancellationResult):
                if (
                    previous.cancel_request_id == request_id
                    and previous.reason == cancellation_reason
                    and previous.cancel_actor == actor
                ):
                    return service._view(job)
                raise RecipeRequestInvalid(
                    "cancellation request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
            children = tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == job.id)
                    .with_for_update(of=AgentOperation)
                )
            )
            for child in children:
                if (
                    child.state == LifecycleState.QUEUED.value
                    and child.current_attempt == 0
                ):
                    AgentOperationAdapter(session).record_outcome(
                        child, None, job, Outcome.CANCELLED, now
                    )
            if any(
                child.state
                in job_states.words(
                    LifecycleState.RUNNING, LifecycleState.NEEDS_OPERATOR
                )
                for child in children
            ):
                job.result = _validated_result(
                    job.kind,
                    {
                        **(
                            json.loads(canonical_message(previous))
                            if previous is not None
                            else {}
                        ),
                        "cancel_requested": True,
                        "cancel_request_id": request_id,
                        "cancel_actor": actor,
                        "cancel_requested_at": _aware(now).isoformat(),
                        "reason": cancellation_reason,
                    },
                ).model_dump(mode="json")
                job.status_reason = cancellation_reason
                job.updated_at = now
                return service._view(job)
            RecipeOperationAdapter().cancelled(
                job, now, reason=cancellation_reason, keep=False
            )
            job.result = _validated_result(
                job.kind,
                {
                    **(
                        _recorded_result_document(
                            job.kind, read_row_column(job, "result"), subject=job.id
                        ).model_dump(mode="json")
                        or {}
                    ),
                    LifecycleState.CANCELLED.value: True,
                    "cancel_requested": True,
                    "cancel_request_id": request_id,
                    "cancel_actor": actor,
                    "cancel_requested_at": _aware(now).isoformat(),
                    "reason": cancellation_reason,
                    "recovery": "retry creates a new operation",
                },
            ).model_dump(mode="json")
            job.updated_at = now
        return service.get(operation_id)

    def _heal_cancelling_build(self, job_id: str) -> None:
        """A legacy build parked ``waiting-for-operator`` by a completion that raced
        its cancel is shown as cancelling again; the sweep below completes it."""
        service = typing_cast("RecipeOperationService", self)
        now = service._clock()
        with service._sessions.begin() as session:
            job = session.get(Job, job_id, with_for_update={"skip_locked": True})
            if job is not None:
                RecipeOperationAdapter().heal(job, now)

    def reconcile_cancelled_builds(self) -> bool:
        """Cancel unneeded dependent builds and resume exact issued cleanup."""
        service = typing_cast("RecipeOperationService", self)
        with service._sessions() as session:
            eligible = (
                select(Job)
                .where(
                    Job.kind == WireAgentOperation.RECIPE_BUILD.value,
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    ),
                    or_(
                        _JsonFlagIsTrue(Job.result, "cancel_requested").is_(True),
                        Job.payload["build_intent"]["kind"].as_string() == "dependency",
                    ),
                )
                .order_by(Job.id)
                .limit(32)
            )
            if service._build_cleanup_cursor is None:
                candidates = tuple(session.scalars(eligible))
            else:
                following = tuple(
                    session.scalars(
                        eligible.where(Job.id > service._build_cleanup_cursor)
                    )
                )
                candidates = following + tuple(
                    session.scalars(
                        eligible.where(Job.id <= service._build_cleanup_cursor).limit(
                            32 - len(following)
                        )
                    )
                )
        if candidates:
            service._build_cleanup_cursor = candidates[-1].id
        progressed = False
        for job in candidates:
            try:
                service._heal_cancelling_build(job.id)
                cancellation = build_cancellation(job)
                progressed = (
                    service._cancel_build(
                        job.id,
                        actor=cancellation.cancel_actor
                        if cancellation
                        else "controller:build-dependency",
                        request_id=cancellation.cancel_request_id
                        if cancellation
                        else str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL, f"vonk:unused-build:{job.id}"
                            )
                        ),
                        reason=cancellation.reason
                        if cancellation
                        else "No current accepted consumer needs this build",
                        only_if_unneeded=True,
                    )
                    or progressed
                )
            except (
                BuildConsumerError,
                RecipeOperationConflict,
                KeyError,
                TypeError,
                ValueError,
            ) as error:
                # Contention/malformed ownership on one execution cannot starve
                # other eligible cleanup. Every decision is rechecked in its
                # own short transaction on the next fair scheduler pass.
                logging.getLogger(__name__).info(
                    "build cleanup deferred for %s: %s", job.id, str(error)
                )
        return progressed
