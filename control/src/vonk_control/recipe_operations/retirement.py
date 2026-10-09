"""Retirement for digest-bound recipe operations."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from vonk_agent_protocol import (
    AgentFailureKind,
    AgentFailureResult,
    InstallationState,
    LifecycleState,
    ReservationState,
    RunState,
    SecurityRefusalReason,
    validate_result_for_operation,
)
from vonk_agent_protocol import AgentOperation as WireAgentOperation

from .. import job_states
from ..agent_jobs import (
    _JsonFlagIsTrue,
)
from ..agent_operation_facts import SUPERSEDED_CANCELLATION_SECONDS
from ..lifecycle.evidence import (
    BookkeepingReason,
    retire_as_unknown,
)
from ..logging import redact_text
from ..models import (
    AgentOperation,
    AgentOperationAttempt,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from ..recipe_progress import (
    _parent_identity as _parent_identity,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_routes import (
    RecipeRouteError,
)
from ..stored_json import read_row_column
from ..strict_json import serialize_json_value
from .errors import RecipeOperationConflict
from .intent import _intent_is_current, _job_workload_intent
from .observation_helpers import _aware
from .results import _recorded_result_document, _validated_result

if TYPE_CHECKING:
    from .service import RecipeOperationService


class RetirementMixin:
    def reconcile_retired_operations(self) -> bool:
        """Resume retired owners through the ordinary exact cleanup path.

        A failed job with typed completed cancellation is the retirement
        handoff. Successful cleanup permits a fresh request through the
        cancellation contract's recovery disposition. Five seconds bounds retry
        traffic per owner; the original job reports the next reconciliation time. No
        execution slot or SQL transaction spans the child admission call.
        """
        service = typing_cast("RecipeOperationService", self)
        now = service._clock()
        interval = timedelta(seconds=5)
        with service._sessions() as session:
            candidates = tuple(
                session.scalars(
                    select(Job)
                    .where(
                        Job.kind.in_(
                            {
                                WireAgentOperation.RECIPE_START.value,
                                WireAgentOperation.RECIPE_STOP.value,
                                WireAgentOperation.RECIPE_INSTALL.value,
                                WireAgentOperation.RECIPE_UNINSTALL.value,
                            }
                        ),
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.FAILED, LifecycleState.CANCELLED
                            )
                        ),
                        _JsonFlagIsTrue(Job.result, LifecycleState.CANCELLED.value),
                        _JsonFlagIsTrue(Job.result, "cancel_requested"),
                        Job.result["recovery"].as_string().is_(None),
                        Job.updated_at <= now - interval,
                    )
                    .order_by(Job.updated_at, Job.id)
                    # At most one child admission/publication per worker tick,
                    # matching the route worker's one-effect scheduling unit.
                    # Updating oldest-first candidates rotates blocked owners.
                    .limit(1)
                )
            )
        progressed = False
        for job in candidates:
            completed = False
            recorded = _recorded_result_document(
                job.kind, read_row_column(job, "result"), subject=job.id
            )
            cancelled_at = getattr(recorded, "cancel_requested_at", None)
            began = (
                _aware(cancelled_at)
                if cancelled_at is not None
                else _aware(job.created_at)
            )
            ended = _aware(now) >= began + timedelta(
                seconds=SUPERSEDED_CANCELLATION_SECONDS
            )
            request_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"vonk:retirement-cleanup:{job.id}")
            )
            try:
                owner_id = _parent_identity(job, "owner_id")
                with service._sessions() as probe:
                    ordinal = _job_workload_intent(probe, job)
                kind = (
                    WireAgentOperation.RECIPE_STOP.value
                    if job.kind
                    in {
                        WireAgentOperation.RECIPE_START.value,
                        WireAgentOperation.RECIPE_STOP.value,
                    }
                    else WireAgentOperation.RECIPE_UNINSTALL.value
                )
                owner_kind = (
                    "run"
                    if kind == WireAgentOperation.RECIPE_STOP.value
                    else "installation"
                )
                if (
                    owner_id is None
                    or ordinal is None
                    or _parent_identity(job, "owner_kind") != owner_kind
                ):
                    # The retired operation lost its owner or its workload intent:
                    # a cleanup is never issued without them (it would take a
                    # newer intent), and the damage is recorded as unknown.
                    retire_as_unknown(
                        "recipe.retirement",
                        job.id,
                        BookkeepingReason.PERSISTED_STATE_DAMAGED,
                        "retired operation owner or workload intent is not provable",
                    )
                    reason = (
                        "exact cleanup blocked: the retired operation's owner or "
                        "workload intent is not provable"
                    )
                elif ended:
                    reason = (
                        "exact cleanup observation ended; unconfirmed capacity retained"
                    )
                else:
                    reason, completed, advanced = service._retirement_cleanup(
                        job, request_id, kind, owner_kind, owner_id, ordinal
                    )
                    progressed = progressed or advanced
            except (
                RecipeOperationConflict,
                RecipeRouteError,
                ValueError,
                TypeError,
                KeyError,
                OSError,
            ) as error:
                # One unavailable or invalid owner never starves unrelated
                # cleanup. The normal admission path retains its blockers.
                reason = f"exact cleanup blocked: {redact_text(str(error))}"
            with service._sessions.begin() as session:
                stored = session.get(Job, job.id, with_for_update=True)
                if (
                    stored is not None
                    and stored.state
                    in job_states.words(LifecycleState.FAILED, LifecycleState.CANCELLED)
                    and read_row_column(stored, "result")
                    == read_row_column(job, "result")
                ):
                    stored.status_reason = (
                        f"operator retired; {reason}"
                        + (
                            ""
                            if completed or ended
                            else f"; next reconciliation at {(now + interval).isoformat()}"
                        )
                    )[:1024]
                    if completed or ended:
                        progressed = True
                        stored.result = serialize_json_value(
                            _validated_result(
                                stored.kind,
                                {
                                    **(
                                        serialize_json_value(
                                            _recorded_result_document(
                                                stored.kind,
                                                read_row_column(stored, "result"),
                                                subject=stored.id,
                                            )
                                        )
                                        or {}
                                    ),
                                    "recovery": "retry creates a new operation",
                                },
                            )
                        )
                    stored.updated_at = now
        return progressed

    def _retirement_cleanup(
        self,
        job: Job,
        request_id: str,
        kind: str,
        owner_kind: str,
        owner_id: str,
        ordinal: int,
    ) -> tuple[str, bool, bool]:
        """Resume one retired owner's exact cleanup: (reason, completed, advanced)."""
        service = typing_cast("RecipeOperationService", self)

        completed = False
        progressed = False
        denied = False
        with service._sessions.begin() as session:
            existing = service._idempotent_in_session(
                session,
                request_id,
                kind,
                None,
                owner_kind=owner_kind,
                owner_id=owner_id,
            )
            if existing is not None:
                latest = session.scalar(
                    select(Job)
                    .where(
                        Job.kind == kind,
                        Job.payload["owner_kind"].as_string() == owner_kind,
                        Job.payload["owner_id"].as_string() == owner_id,
                        Job.payload["workload_intent_ordinal"].as_integer() == ordinal,
                    )
                    .order_by(Job.created_at.desc(), Job.id.desc())
                    .limit(1)
                )
                if latest is not None:
                    existing = service._view(latest, session=session)
                for child, attempt in session.execute(
                    select(AgentOperation, AgentOperationAttempt)
                    .join(
                        AgentOperationAttempt,
                        AgentOperationAttempt.operation_id == AgentOperation.id,
                    )
                    .where(
                        AgentOperation.parent_job_id == existing.id,
                        AgentOperationAttempt.attempt == AgentOperation.current_attempt,
                        AgentOperationAttempt.state == LifecycleState.FAILED.value,
                    )
                ):
                    if read_row_column(attempt, "result") is None:
                        continue
                    try:
                        evidence = validate_result_for_operation(
                            child.kind,
                            read_row_column(attempt, "result"),
                            state=LifecycleState.FAILED.value,
                        )
                    except (TypeError, ValueError):
                        # Unreadable evidence does not establish a security denial.
                        continue
                    if isinstance(
                        evidence, AgentFailureResult
                    ) and evidence.failure_kind in {
                        AgentFailureKind.INVALID_AUTHORITY,
                        AgentFailureKind.INVALID_CONTRACT,
                        AgentFailureKind.INTEGRITY_FAILURE,
                    }:
                        denied = True
            current = _intent_is_current(session, ordinal, job.targets)
            owner = session.get(
                RecipeRun
                if kind == WireAgentOperation.RECIPE_STOP.value
                else RecipeInstallation,
                owner_id,
            )
            # A missing owner is not evidence of a stopped executor. Exact
            # cleanup receipts can retire its scoped claims independently.
            if owner is None and existing is not None:
                for child, attempt in session.execute(
                    select(AgentOperation, AgentOperationAttempt)
                    .join(
                        AgentOperationAttempt,
                        AgentOperationAttempt.operation_id == AgentOperation.id,
                    )
                    .where(
                        AgentOperation.parent_job_id == existing.id,
                        AgentOperation.kind == kind,
                        AgentOperationAttempt.attempt == AgentOperation.current_attempt,
                        AgentOperationAttempt.state == LifecycleState.SUCCEEDED,
                    )
                ):
                    try:
                        validate_result_for_operation(
                            kind,
                            read_row_column(attempt, "result"),
                            state=LifecycleState.SUCCEEDED,
                        )
                    except (TypeError, ValueError):
                        continue
                    for reservation in session.scalars(
                        select(ResourceReservation).where(
                            ResourceReservation.owner_kind == owner_kind,
                            ResourceReservation.owner_id == owner_id,
                            ResourceReservation.node_id == child.node_id,
                            ResourceReservation.state == ReservationState.ACTIVE,
                        )
                    ):
                        reservation.state = ReservationState.RELEASED
                        reservation.released_at = service._clock()
            completed = (
                owner is None
                and existing is not None
                or owner is not None
                and owner.state
                == (
                    RunState.STOPPED
                    if kind == WireAgentOperation.RECIPE_STOP.value
                    else InstallationState.UNINSTALLED
                )
            ) and session.scalar(
                select(ResourceReservation.id)
                .where(
                    ResourceReservation.owner_kind
                    == (
                        "run"
                        if kind == WireAgentOperation.RECIPE_STOP.value
                        else "installation"
                    ),
                    ResourceReservation.owner_id == owner_id,
                    ResourceReservation.state == ReservationState.ACTIVE,
                )
                .limit(1)
            ) is None
        if completed:
            reason = "exact cleanup confirmed; capacity released"
        elif owner is None:
            # Child receipt observation continues in the queue owner. No
            # destructive dispatch is synthesized from missing bookkeeping.
            reason = (
                "cleanup owner projection unavailable; exact capacity remains reserved"
            )
        elif not current:
            reason = "newer workload intent owns cleanup; uncertain capacity remains reserved"
        elif denied:
            reason = f"{SecurityRefusalReason.STALE_FENCE}: exact cleanup authorization is unavailable"
        elif existing is not None and existing.state not in job_states.words(
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
            LifecycleState.NEEDS_OPERATOR,
        ):
            reason = f"exact cleanup {existing.id} is {existing.state}; capacity retained until its receipt"
        else:
            if existing is not None:
                request_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{existing.id}:exact-cleanup-retry")
                )
            if kind == WireAgentOperation.RECIPE_STOP.value:
                plan = service.preview_stop(owner_id)
                cleanup = service.stop(
                    owner_id,
                    plan_digest=plan.plan_digest,
                    actor=job.actor,
                    request_id=request_id,
                    workload_intent_ordinal=ordinal,
                )
            else:
                plan = service.preview_uninstall(owner_id)
                cleanup = service.uninstall(
                    owner_id,
                    plan_digest=plan.plan_digest,
                    actor=job.actor,
                    request_id=request_id,
                    workload_intent_ordinal=ordinal,
                )
            progressed = True
            reason = f"exact cleanup {cleanup.id} is {cleanup.state}; capacity retained until its receipt"
        return reason, completed, progressed
