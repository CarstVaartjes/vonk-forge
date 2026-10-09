"""Bounded recovery policies for the node-scoped agent queue."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, object_session
from sqlalchemy.sql.elements import ColumnElement
from vonk_agent_protocol import (
    AgentFailureKind,
    AgentOperation,
    AgentResultState,
    DistributionAssignmentState,
    FailureStage,
    HelperErrorCode,
    LifecycleState,
    validate_result_for_operation,
)
from vonk_agent_protocol.contracts import canonical_payload
from vonk_agent_protocol.wire_model import WireModel

from .. import agent_operation_states
from ..agent_operation_facts import (
    AGENT_UPGRADE_RECOVERY_FENCE,
    SUPERSEDED_CANCELLATION_SECONDS,
)
from ..agent_operation_facts import (
    RESTART_REISSUE_OPERATIONS as _RESTART_REISSUE_OPERATIONS,
)
from ..agent_operation_facts import aware as _aware
from ..agent_upgrade_status import AGENT_UPGRADE_AWAITING_IDENTITY_REASONS
from ..lifecycle import CancelRequested, Outcome, Reported
from ..lifecycle.agent_operation import (
    AgentOperationAdapter,
    retry_scheduled,
    set_parent_state,
)
from ..models import AgentOperation as StoredOperation
from ..models import AgentOperationAttempt, ArtifactDistributionAssignment, Job
from ..recovery_policy import (
    FailureKind,
    RecoveryDecision,
    classify,
    kind_for_agent_error,
)
from .contracts import _ABANDONABLE_OPERATIONS, _ENDED_PARENT_STATES, _GRANT_LIFETIME
from .stored import column_field, column_is_document, column_value


def superseded_cancellation_deadline(result: object) -> datetime | None:
    """Fixed cap for an old fence's cancellation-only STOP authority."""
    if isinstance(result, BaseModel):
        if getattr(result, "cancel_requested", None) is not True:
            return None
        value = getattr(result, "cancel_requested_at", None)
    elif isinstance(result, Mapping):
        # Explicit generic-job passthrough; production results are models.
        if result.get("cancel_requested") is not True:
            return None
        value = result.get("cancel_requested_at")
    else:
        return None
    if isinstance(value, datetime):
        requested_at = value
    elif isinstance(value, str):
        try:
            requested_at = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if requested_at.tzinfo is None:
        return None
    return requested_at + timedelta(seconds=SUPERSEDED_CANCELLATION_SECONDS)


def _safe_retry_failure(kind: str, state: str, result: WireModel) -> bool:
    """One classification for both fresh results and retained interrupted work.

    Every restart-safe order retries automatically, with a bounded rate, while
    its intent is current: a transient dependency failure is re-issued, and an
    uncertain effect is re-issued through the exact-resume path that inspects
    and reconciles the prior effect first.  Invalid contracts, denied
    authority, and integrity refusals are not transient and stay terminal.
    """
    if kind == AgentOperation.RECIPE_JOB_RUN.value:
        # Only a positively pre-execution report can reissue an irreversible
        # job. Uncertain runtime effects still go through observation.
        return (
            state == AgentResultState.FAILED.value
            and result.get("stage") == FailureStage.MODEL_MATERIALIZATION.value
            and kind_for_agent_error(result) is AgentFailureKind.TEMPORARY_DEPENDENCY
        )
    if kind == AgentOperation.AGENT_UPGRADE.value:
        # This observation guarantees no dpkg or rollback activation occurred.
        # It preserves the same durable order/package and uses the core budget.
        return (
            result.get("helper_error_code")
            == HelperErrorCode.PACKAGE_PREPARATION_UNAVAILABLE.value
            and state == LifecycleState.FAILED.value
        )
    if kind not in _RESTART_REISSUE_OPERATIONS:
        return False
    if state == agent_operation_states.WIRE_UNKNOWN:
        # The agent's own words for "I could not confirm the effect".  Its body
        # is often only ``{"reason": ...}`` with no ``failure_kind``, which
        # ``kind_for_agent_error`` reads as an invalid contract, so the kind is
        # not consulted: any waiting result of a restart-safe order is an
        # uncertain effect, re-issued through the exact-resume path that
        # inspects and reconciles it first (a stop that could not be confirmed
        # is idempotent and must not wait for a person).
        return True
    failure_kind = kind_for_agent_error(result)
    return (
        state == "failed"
        and result.get("status") == "failed"
        and (
            classify(failure_kind) is RecoveryDecision.RETRY
            # A missing resource prerequisite is a temporary shortage: wait
            # for it with a visible next attempt instead of a terminal failure.
            or failure_kind is FailureKind.RESOURCE_PREREQUISITE
        )
    )


def _retry_authorized_for_current_attempt(operation: StoredOperation) -> bool:
    """Whether a retry is already scheduled for the operation's current attempt.

    The claim that issues an attempt clears ``next_action_at``, so a schedule
    can only belong to the attempt that is parked now; an earlier attempt's
    never authorises, schedules, or hides the current attempt's recovery.  (A
    row written before the core carries its schedule in the legacy retry
    columns; ``retry_scheduled`` reads both.)
    """
    return retry_scheduled(operation) is not None


def _retry_not_authorized_for_current_attempt() -> ColumnElement[bool]:
    """SQL form of the negation of :func:`_retry_authorized_for_current_attempt`."""
    return or_(
        StoredOperation.next_action_at.is_(None), StoredOperation.observe_count > 0
    )


def _parked_retry_evidence(
    operation: StoredOperation, attempt: AgentOperationAttempt, now: datetime
) -> bool:
    """Prove that a parked current-schema attempt still owns safe recovery."""
    if (
        operation.state not in agent_operation_states.PARKED
        or _retry_authorized_for_current_attempt(operation)
        or operation.current_attempt < 1
        or operation.current_attempt != attempt.attempt
        or operation.kind not in _RESTART_REISSUE_OPERATIONS
    ):
        return False
    try:
        payload = canonical_payload(
            AgentOperation(operation.kind), column_value(operation, "payload")
        )
    except (TypeError, ValueError):
        return False
    if hashlib.sha256(payload).hexdigest() != operation.payload_digest:
        return False
    if agent_operation_states.attempt_lapsed(attempt):
        # Expiry never proves the old executor stopped. Only exact-resume
        # operations qualify, whose agent reconciles the old effect first.
        # A late result retained under the expired fence stays diagnostic
        # evidence; it is not a reason to park. It proves only that the old
        # executor finished, which makes the exact re-issue safer. Requiring
        # its absence parked a start whose result arrived after its lease
        # behind an operator forever.
        return _aware(attempt.lease_deadline) <= _aware(now)
    if not agent_operation_states.attempt_failed_or_unknown(attempt):
        return False
    try:
        result = validate_result_for_operation(
            operation.kind,
            column_value(attempt, "result"),
            state=agent_operation_states.attempt_wire_state(attempt),
        )
    except (TypeError, ValueError):
        return False
    if not isinstance(result, WireModel):
        return False
    return _safe_retry_failure(
        operation.kind, agent_operation_states.attempt_wire_state(attempt), result
    )


def _abandon_operation(
    operation: StoredOperation,
    job_id: str,
    now: datetime,
    *,
    reason: str | None = None,
) -> None:
    """Close a parked idempotent operation whose job has already ended.

    Left parked it would advertise a retry that the ended job can never grant
    a claim for, and no operator action applies to it.
    """

    session = object_session(operation)
    assert session is not None
    AgentOperationAdapter(session).settle(
        operation,
        None,
        None,
        CancelRequested(
            reason=reason
            or f"job {job_id} ended without this operation; its retry was abandoned"
        ),
        now,
    )


def abandon_idempotent_job_in_session(
    session: Session, job_id: str, now: datetime, *, reason: str
) -> bool:
    """Close a job of idempotent operations that its owner no longer wants.

    Parked and not-yet-issued operations are cancelled, then the job. Whatever
    the transfers already left on the Spark (finished objects, partial files)
    stays for the next request to reuse. Returns ``False``, changing nothing,
    when the job holds another kind of operation or one that is still running:
    that one reports its own outcome.
    """

    job = session.scalar(select(Job).where(Job.id == job_id).with_for_update(of=Job))
    if job is None:
        return False
    operations = tuple(
        session.scalars(
            select(StoredOperation)
            .where(StoredOperation.parent_job_id == job_id)
            .with_for_update(of=StoredOperation)
        )
    )
    if not operations or any(
        operation.kind not in _ABANDONABLE_OPERATIONS or operation.state == "running"
        for operation in operations
    ):
        return False
    adapter = AgentOperationAdapter(session)
    for operation in operations:
        if operation.state in agent_operation_states.QUEUED_OR_PARKED:
            adapter.settle(operation, None, job, CancelRequested(reason=reason), now)
    if job.state not in _ENDED_PARENT_STATES:
        set_parent_state(job, "cancelled", reason, now)
    return True


def _renew_distribution_grant(
    session: Session,
    operation: StoredOperation,
    now: datetime,
    *,
    live_only: bool = False,
) -> None:
    """Extend the exact node/plan grant of a currently authorised transfer.

    The grant bounds how long an unattended registration may serve bytes; the
    claim (or a heartbeat of the fenced attempt) is the proof that its
    operation is still authorised. A grant that merely ran out of time is
    therefore renewed by the next attempt, while a revoked one stays revoked.
    Renewal is sparse. ``live_only`` (heartbeats) never revives a grant that
    already lapsed.
    """

    states = (
        {DistributionAssignmentState.ACTIVE}
        if live_only
        else {DistributionAssignmentState.ACTIVE, DistributionAssignmentState.EXPIRED}
    )
    conditions = [
        ArtifactDistributionAssignment.node_id == operation.node_id,
        ArtifactDistributionAssignment.plan_digest
        == column_field(operation, "payload", "plan_digest"),
        ArtifactDistributionAssignment.state.in_(states),
        ArtifactDistributionAssignment.expires_at < now + timedelta(minutes=30),
    ]
    if live_only:
        conditions.append(ArtifactDistributionAssignment.expires_at > now)
    session.execute(
        update(ArtifactDistributionAssignment)
        .where(*conditions)
        .values(state="active", expires_at=now + _GRANT_LIFETIME, updated_at=now)
    )


def schedule_agent_upgrade_retry(
    operation: StoredOperation,
    attempt: AgentOperationAttempt | None,
    now: datetime,
) -> None:
    """Schedule one agent-upgrade order for automatic, fenced re-dispatch.

    The retry is claimable only after the safety fence and only while the Spark
    still runs the exact rollback source (see ``_claim_once``); authenticated
    contact that proves the target completes the order first.  The lifecycle
    core decides it (a retryable failure of a restart-safe order is a retry);
    the adapter owns the fence.
    """

    session = object_session(operation)
    assert session is not None
    adapter = AgentOperationAdapter(session)
    if operation.state in {"succeeded", "failed"}:
        # A helper acknowledgement (or a failure the owner treats as retryable)
        # is not the end of an upgrade: only authenticated contact that proves
        # the target is.  The order is reopened so the core can schedule it.
        adapter.reopen(operation, now)
    adapter.settle(
        operation,
        attempt,
        session.get(Job, operation.parent_job_id),
        Reported(Outcome.FAILED, retryable=True),
        now,
    )


def agent_upgrade_in_flight(
    session: Session, operation: StoredOperation, now: datetime
) -> bool:
    """Whether this agent-upgrade order still occupies the fleet's one slot.

    A dispatched install is in flight until its attempt reports, or until the
    dpkg safety fence has elapsed after its lease (a Spark that went dark
    mid-install cannot hold every other Spark forever).  A handed-off install
    awaiting its new identity stays in flight until its fence elapses.  An
    explicit failure retries later but does not hold the fleet.
    """

    if operation.kind != AgentOperation.AGENT_UPGRADE.value:
        return False
    if operation.state not in agent_operation_states.RUNNING_OR_PARKED:
        return False
    if operation.current_attempt < 1:
        return False
    attempt = session.scalar(
        select(AgentOperationAttempt).where(
            AgentOperationAttempt.operation_id == operation.id,
            AgentOperationAttempt.attempt == operation.current_attempt,
        )
    )
    if attempt is None:
        return operation.state == "running"
    deadline = _aware(attempt.lease_deadline)
    current = _aware(now)
    if operation.state == "running":
        return deadline + AGENT_UPGRADE_RECOVERY_FENCE > current
    if deadline <= current:
        return False
    reason = (
        column_field(attempt, "result", "reason")
        if column_is_document(attempt, "result")
        else None
    )
    return agent_operation_states.attempt_is_observing(attempt) or (
        reason in AGENT_UPGRADE_AWAITING_IDENTITY_REASONS
    )


def other_agent_upgrade_in_flight(
    session: Session, operation: StoredOperation, now: datetime
) -> bool:
    """Whether any other Spark's agent upgrade, in any rollout, is in flight."""

    return any(
        agent_upgrade_in_flight(session, other, now)
        for other in session.scalars(
            select(StoredOperation).where(
                StoredOperation.kind == AgentOperation.AGENT_UPGRADE.value,
                StoredOperation.id != operation.id,
                StoredOperation.state.in_(agent_operation_states.RUNNING_OR_PARKED),
            )
        )
    )
