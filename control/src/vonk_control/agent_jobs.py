"""Transactional, node-scoped agent operation queue with lease fencing."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import Boolean, and_, case, or_, select, update
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.functions import FunctionElement
from vonk_agent_protocol import (
    AgentClaim,
    AgentDirective,
    AgentOperation,
    AgentProgress,
    AgentResult,
    canonical_message,
    validate_result_for_operation,
)
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.contracts import canonical_payload
from vonk_agent_protocol.recipe_jobs import RecipeJobRunResult

from .admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    acquire_admission_keys,
    lock_admission_rows,
    node_admission_key,
)
from .agent_upgrade_status import operator_agent_upgrade_reason
from .auth import AgentSource
from .failure_evidence import safe_text, sanitize_diagnostics
from .install_admission import InstallAdmissionBusy
from .logging import redact_text
from .models import (
    AgentCertificate,
    AgentNode,
    AgentNodeProfile,
    AgentOperationAttempt,
    ArtifactDistributionAssignment,
    ArtifactJob,
    Job,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from .models import AgentOperation as StoredOperation
from .operation_contract import sanitize_failure_evidence, validate_progress_update
from .operation_progress import observe_progress, progress_write_due
from .recipe_builds import BUILD_ARTIFACT_FORMAT
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_build_policy,
)
from .recipe_lifecycle_contract import (
    RecipeOperationCancellationResult,
    parse_recipe_lifecycle_result,
)
from .recovery_policy import (
    FailureKind,
    RecoveryDecision,
    RecoveryPolicy,
    classify,
    kind_for_agent_error,
)
from .run_admission import RunAdmissionBusy

AgentFence = str | AgentClaim | AgentProgress | AgentResult
ResultConsumer = Callable[
    [Session, StoredOperation, AgentOperationAttempt, AgentResult], None
]
ContactConsumer = Callable[[Session, AgentSource], None]
# The protocol owns this closed set; the Controller aliases it locally so
# ``_finish`` cannot be handed any string.
AgentResultState = Literal["succeeded", "failed", "cancelled", "waiting-for-operator"]


@dataclass(frozen=True, slots=True)
class SupersededAgentEffect:
    parent_job_id: str
    operation_id: str
    node_id: str
    kind: str
    failure_kind: FailureKind
    observe_due_at: datetime
    observation_deadline: datetime


_RECIPE_CAPABILITIES = frozenset(
    {
        AgentOperation.RECIPE_BUILD.value,
        AgentOperation.RECIPE_BUILD_CLEANUP.value,
        AgentOperation.RECIPE_IMAGE_IMPORT.value,
        AgentOperation.RECIPE_INSTALL.value,
        AgentOperation.ARTIFACT_DISTRIBUTION.value,
        AgentOperation.RECIPE_START.value,
        AgentOperation.RECIPE_JOB_RUN.value,
        AgentOperation.RECIPE_STOP.value,
        AgentOperation.RECIPE_UNINSTALL.value,
        AgentOperation.RECIPE_RECONCILE.value,
    }
)
_MUTATING_OPERATIONS = frozenset(
    {
        AgentOperation.AGENT_UPGRADE.value,
        AgentOperation.RECIPE_BUILD.value,
        AgentOperation.RECIPE_BUILD_CLEANUP.value,
        AgentOperation.RECIPE_IMAGE_IMPORT.value,
        AgentOperation.RECIPE_INSTALL.value,
        AgentOperation.ARTIFACT_DISTRIBUTION.value,
        AgentOperation.RECIPE_START.value,
        AgentOperation.RECIPE_JOB_RUN.value,
        AgentOperation.RECIPE_STOP.value,
        AgentOperation.RECIPE_UNINSTALL.value,
        AgentOperation.RECIPE_RECONCILE.value,
    }
)
_WORKLOAD_INTENT_OPERATIONS = frozenset(
    {
        AgentOperation.RECIPE_IMAGE_IMPORT.value,
        AgentOperation.RECIPE_INSTALL.value,
        AgentOperation.ARTIFACT_DISTRIBUTION.value,
        AgentOperation.RECIPE_START.value,
        AgentOperation.RECIPE_JOB_RUN.value,
        AgentOperation.RECIPE_STOP.value,
        AgentOperation.RECIPE_UNINSTALL.value,
        AgentOperation.RECIPE_RECONCILE.value,
    }
)
_LIFECYCLE_RESTART_OPERATIONS = frozenset(
    {
        AgentOperation.RECIPE_INSTALL.value,
        AgentOperation.RECIPE_START.value,
        AgentOperation.RECIPE_STOP.value,
        AgentOperation.RECIPE_UNINSTALL.value,
        AgentOperation.RECIPE_RECONCILE.value,
    }
)
_RESTART_REISSUE_OPERATIONS = _LIFECYCLE_RESTART_OPERATIONS | frozenset(
    {
        AgentOperation.ARTIFACT_DISTRIBUTION.value,
        AgentOperation.RUNTIME_PREFLIGHT.value,
    }
)
_TERMINAL_PARENT_STATES = frozenset(
    {"succeeded", "failed", "waiting-for-operator", "expired", "cancelled"}
)
#: An outcome that has already concluded, so the work it belongs to will not
#: proceed and no refusal can describe it.  A refusal note written afterwards
#: annotates a finished result: on the operator surface a success then reads as
#: a refusal of work that already completed.  Derived from the aggregate-final
#: set so a new final state cannot be added without deciding whether it may
#: carry a refusal; ``waiting-for-operator`` is excluded because such work can
#: resume, and a refusal that explains why it is not progressing is genuine
#: evidence that must stay.
_AGGREGATE_FINAL_STATES = frozenset(
    {"cancelled", "compensated", "failed", "succeeded", "waiting-for-operator"}
)
_CONCLUDED_OUTCOMES = _AGGREGATE_FINAL_STATES - {"waiting-for-operator"}
_RETRY_DISPOSITION = "retry"
_DATABASE_REPOLL_SECONDS = 0.25
_SUPERSEDED_CANCELLATION_SECONDS = 660


def superseded_cancellation_deadline(result: object) -> datetime | None:
    """Fixed cap for an old fence's cancellation-only STOP authority."""
    if not isinstance(result, Mapping) or result.get("cancel_requested") is not True:
        return None
    value = result.get("cancel_requested_at")
    if not isinstance(value, str):
        return None
    try:
        requested_at = datetime.fromisoformat(value)
    except ValueError:
        return None
    if requested_at.tzinfo is None:
        return None
    return requested_at + timedelta(seconds=_SUPERSEDED_CANCELLATION_SECONDS)


_RUNTIME_CAPABILITIES = frozenset({"agent.runtime.rust.v1", "runtime.vonk.v1"})
EXACT_LIFECYCLE_RESUME_CAPABILITY = "agent.lifecycle.resume.exact.v1"
RECIPE_RECONCILE_FEATURE_CAPABILITY = "recipe.reconcile.v1"
_NEXT_CAPABILITIES = _RUNTIME_CAPABILITIES | _RECIPE_CAPABILITIES
_OPTIONAL_CAPABILITIES = frozenset(
    {
        AgentOperation.AGENT_UPGRADE.value,
        AgentOperation.RUNTIME_PREFLIGHT.value,
        "recipe.start.two-phase.v1",
        "recipe.run.inspect.exact.v1",
        "recipe.run.inspect.receipt.v1",
        EXACT_LIFECYCLE_RESUME_CAPABILITY,
        RECIPE_RECONCILE_FEATURE_CAPABILITY,
    }
)
_KNOWN_CAPABILITIES = _NEXT_CAPABILITIES | _OPTIONAL_CAPABILITIES
_CONTROL_OPERATIONS = (
    _NEXT_CAPABILITIES - _RUNTIME_CAPABILITIES
) | _OPTIONAL_CAPABILITIES


def _safe_retry_failure(kind: str, state: str, result: Mapping[str, object]) -> bool:
    """One classification for both fresh results and retained interrupted work."""
    if kind not in _RESTART_REISSUE_OPERATIONS:
        return False
    if state == "waiting-for-operator":
        return (
            result.get("error_code") == "agent_restart_interrupted"
            and kind_for_agent_error(result) is FailureKind.UNCERTAIN_EFFECT
            and result.get("uncertain") is True
        )
    if (
        state != "failed"
        or result.get("status") != "failed"
        or classify(kind_for_agent_error(result)) is not RecoveryDecision.RETRY
    ):
        return False
    return kind in {
        AgentOperation.ARTIFACT_DISTRIBUTION.value,
        AgentOperation.RECIPE_STOP.value,
        AgentOperation.RECIPE_UNINSTALL.value,
        AgentOperation.RECIPE_RECONCILE.value,
    } or (
        kind == AgentOperation.RECIPE_START.value
        and result.get("error_code") == "runtime_observation_unavailable"
    )


def _parked_retry_evidence(
    operation: StoredOperation, attempt: AgentOperationAttempt, now: datetime
) -> bool:
    """Prove that a parked current-schema attempt still owns safe recovery."""
    if (
        operation.state != "waiting-for-operator"
        or operation.retry_disposition is not None
        or operation.retry_disposition_attempt is not None
        or operation.retry_due_at is not None
        or operation.current_attempt < 1
        or operation.current_attempt != attempt.attempt
        or operation.kind not in _RESTART_REISSUE_OPERATIONS
    ):
        return False
    try:
        payload = canonical_payload(AgentOperation(operation.kind), operation.payload)
    except (TypeError, ValueError):
        return False
    if hashlib.sha256(payload).hexdigest() != operation.payload_digest:
        return False
    if attempt.state == "expired":
        # Expiry never proves the old executor stopped. Only exact-resume
        # operations qualify, whose agent reconciles the old effect first.
        return attempt.result is None and _aware(attempt.lease_deadline) <= _aware(now)
    if attempt.state not in {"failed", "waiting-for-operator"}:
        return False
    try:
        result = validate_result_for_operation(
            operation.kind, attempt.result, state=attempt.state
        ).model_dump(mode="json")
    except (TypeError, ValueError):
        return False
    return _safe_retry_failure(operation.kind, attempt.state, result)


class StaleAgentAttempt(RuntimeError):
    """An agent attempted to update an operation it no longer owns."""


class OperatorRetryExhausted(ValueError):
    """A parked operation cannot be authorised another attempt.

    ``resume`` is the operator action that releases a job parked in
    ``waiting-for-operator``, and the retry authorisation it writes is bounded
    by the same :class:`RecoveryPolicy` budget every other retry path uses.
    The refusal is typed so a caller reports the spent budget instead of
    returning a queued job whose operation can never be claimed.
    """

    def __init__(self, operation_id: str, kind: str, attempt: int, limit: int) -> None:
        self.operation_id = operation_id
        self.kind = kind
        self.attempt = attempt
        self.limit = limit
        super().__init__(
            f"operation {operation_id} ({kind}) exhausted its {limit}-attempt "
            f"retry budget at attempt {attempt}"
        )


class OperatorRetirementRefused(ValueError):
    """An operator asked to retire parked work that is not genuinely exhausted.

    Retirement is the terminal counterpart of ``resume``: it fails a parked
    order whose bounded retry budget is spent, retaining uncertain effects
    for exact cleanup. The same :class:`RecoveryPolicy` decision that refuses an
    over-budget resume decides whether retirement is permitted, and this typed
    refusal names the one condition that still makes the operation live.
    """

    def __init__(self, operation_id: str, reason: str) -> None:
        self.operation_id = operation_id
        self.reason = reason
        super().__init__(f"operation {operation_id} cannot be retired: {reason}")


def retry_due_after_operator_action(
    operation: StoredOperation, now: datetime, policy: RecoveryPolicy | None = None
) -> datetime | None:
    """The one bounded operator-retry decision for one parked operation.

    Both the resume authorisation and the retirement refusal evaluate this
    object, so a change to the budget cannot move one without the other: a
    ``None`` due time *is* "the budget is spent" for each of them.  The caller
    passes the same :class:`RecoveryPolicy` whose ``max_failures`` it reports,
    so the refusal cannot name a different bound from the one it decided.
    """

    return (policy or RecoveryPolicy()).next_attempt(
        operation.id, operation.current_attempt, _aware(now)
    )


def release_owned_reservations_in_session(
    session: Session, owner_kind: str, owner_id: str, now: datetime
) -> None:
    """Release every active resource reservation an operation owner holds."""

    for reservation in session.scalars(
        select(ResourceReservation).where(
            ResourceReservation.owner_kind == owner_kind,
            ResourceReservation.owner_id == owner_id,
            ResourceReservation.state == "active",
        )
    ):
        reservation.state = "released"
        reservation.released_at = now


def operator_resume_candidates_in_session(
    session: Session, job_id: str, now: datetime
) -> tuple[StoredOperation, ...]:
    """Return the parked children whose current Job owner still has intent.

    The Job projection and the resume mutation share this owner and intent
    decision. Retry budget is applied by the latter as a typed refusal and by
    the former as an absent action.
    """

    job = session.get(Job, job_id)
    if job is None or job.state not in {"queued", "running", "waiting-for-operator"}:
        return ()
    if job.result is not None:
        if not isinstance(job.result, Mapping):
            return ()
        cancel_requested = job.result.get("cancel_requested")
        if cancel_requested is not None and cancel_requested is not False:
            return ()
    scope = AgentJobService._target_scope(job.targets)
    if scope is None:
        return ()
    payload = job.payload if isinstance(job.payload, Mapping) else None
    if payload is None:
        return ()
    parent_intent = payload.get("workload_intent_ordinal")
    if parent_intent is not None and (
        type(parent_intent) is not int or parent_intent < 1
    ):
        return ()
    candidates = tuple(
        session.scalars(
            select(StoredOperation)
            .where(
                StoredOperation.parent_job_id == job_id,
                StoredOperation.state == "waiting-for-operator",
            )
            .order_by(StoredOperation.id)
        )
    )
    if not candidates:
        return ()
    nodes = {
        node.node_id: node
        for node in session.scalars(
            select(AgentNode).where(AgentNode.node_id.in_(scope))
        )
    }
    # A topology-wide request cannot resume only its still-current subset.
    # The mutation locks this complete target scope before this shared decision.
    if set(nodes) != set(scope) or any(
        node.state != "active"
        or node.revoked_at is not None
        or (parent_intent is not None and node.workload_intent_ordinal != parent_intent)
        for node in nodes.values()
    ):
        return ()
    eligible: list[StoredOperation] = []
    for operation in candidates:
        if operation.node_id not in scope:
            continue
        node = nodes.get(operation.node_id)
        if node is None:
            continue
        if operation.current_attempt < 1:
            continue
        if parent_intent is None:
            if operation.workload_intent_ordinal is not None:
                continue
        elif operation.workload_intent_ordinal != parent_intent:
            continue
        if (
            operation.kind in _WORKLOAD_INTENT_OPERATIONS
            and operation.workload_intent_ordinal is None
        ):
            continue
        eligible.append(operation)
    return tuple(eligible)


def operator_resume_eligible_operations_in_session(
    session: Session, job_id: str, now: datetime
) -> tuple[StoredOperation, ...]:
    """Return the current-owner resume action when its bounded budget remains."""

    job = session.get(Job, job_id)
    if job is None or job.state != "waiting-for-operator":
        return ()
    policy = RecoveryPolicy()
    return tuple(
        operation
        for operation in operator_resume_candidates_in_session(session, job_id, now)
        if not (
            operation.retry_disposition == _RETRY_DISPOSITION
            and operation.retry_disposition_attempt == operation.current_attempt
        )
        and retry_due_after_operator_action(operation, now, policy) is not None
    )


def authorize_operator_resume_in_session(
    session: Session, job_id: str, now: datetime
) -> None:
    """Authorise one bounded claim for each parked operation of ``job_id``.

    The claim predicate requires a ``waiting-for-operator`` operation to carry
    its own retry authorisation, so releasing the parent job alone leaves the
    operation unclaimable and an operator resume that wrote only the parent
    state silently did nothing.  This writes the very disposition the
    exact-resume path writes, due immediately, and refuses once the operation
    has spent :class:`RecoveryPolicy`'s attempt budget so a resume cannot be
    replayed into an unbounded retry loop.  It performs no external work, so it
    is safe inside the caller's SQL transaction.
    """

    operations = operator_resume_candidates_in_session(session, job_id, now)
    if not operations:
        raise ValueError("job has no current authorized resume action")
    operations = tuple(
        session.scalars(
            select(StoredOperation)
            .where(StoredOperation.id.in_([operation.id for operation in operations]))
            .order_by(StoredOperation.id)
            .with_for_update(of=StoredOperation)
        )
    )
    parent = session.get(Job, job_id)
    if parent is None or (
        parent.state != "waiting-for-operator"
        and any(
            operation.retry_disposition != _RETRY_DISPOSITION
            or operation.retry_disposition_attempt != operation.current_attempt
            for operation in operations
        )
    ):
        raise ValueError("job is not waiting for operator")
    policy = RecoveryPolicy()
    for operation in operations:
        if (
            operation.retry_disposition == _RETRY_DISPOSITION
            and operation.retry_disposition_attempt == operation.current_attempt
        ):
            # Already authorised at this attempt: resume is idempotent and must
            # neither spend budget nor move a scheduled retry earlier.
            continue
        if retry_due_after_operator_action(operation, now, policy) is None:
            raise OperatorRetryExhausted(
                operation.id,
                operation.kind,
                operation.current_attempt,
                policy.max_failures,
            )
        operation.retry_disposition = _RETRY_DISPOSITION
        operation.retry_disposition_attempt = operation.current_attempt
        # The operator's authorisation is due now, but the due time must still
        # be set: the parent aggregate treats an authorised retry without one as
        # unparked and would return the job to ``waiting-for-operator`` before
        # the agent polls.
        operation.retry_due_at = now
        operation.status_reason = (
            f"operator resumed; attempt {operation.current_attempt + 1} of "
            f"{policy.max_failures} authorised"
        )[:512]
        operation.updated_at = now


def retire_exhausted_operations_in_session(
    session: Session, job_id: str, now: datetime
) -> tuple[str, ...]:
    """Fence exhausted orders while retaining their effects for exact cleanup.

    This is the bounded, audited terminal counterpart to
    :func:`authorize_operator_resume_in_session`.  It is admitted only when
    every parked operation of the job has spent :class:`RecoveryPolicy`'s
    attempt budget *and* holds no live attempt lease and no already-authorised
    retry or open launch budget. An expired lease does not prove the effect
    stopped. The worker resumes the ordinary stop/uninstall path from durable
    cancellation facts; only its successful receipt releases reservations.
    """

    job = session.get(Job, job_id)
    if job is None:
        raise KeyError(job_id)
    scope = AgentJobService._target_scope(job.targets)
    if not scope or not AgentJobService._lock_target_scopes(
        session, {"retire": (job_id, scope)}, scope[0]
    ):
        raise OperatorRetirementRefused(job_id, "its target scope changed")
    if job.state != "waiting-for-operator":
        raise OperatorRetirementRefused(job_id, "job is not waiting for operator")
    if (
        session.scalar(
            select(StoredOperation.id)
            .where(
                StoredOperation.parent_job_id == job_id,
                or_(
                    StoredOperation.state == "running",
                    and_(
                        StoredOperation.state == "queued",
                        StoredOperation.current_attempt > 0,
                    ),
                ),
            )
            .limit(1)
        )
        is not None
    ):
        raise OperatorRetirementRefused(
            job_id, "another issued operation is still active"
        )
    operations = tuple(
        session.scalars(
            select(StoredOperation)
            .where(
                StoredOperation.parent_job_id == job_id,
                StoredOperation.state == "waiting-for-operator",
            )
            .order_by(StoredOperation.id)
            .with_for_update(of=StoredOperation)
        )
    )
    if not operations:
        raise OperatorRetirementRefused(job_id, "job has no parked operation")
    policy = RecoveryPolicy()
    attempts: list[AgentOperationAttempt] = []
    for operation in operations:
        if retry_due_after_operator_action(operation, now, policy) is not None:
            raise OperatorRetirementRefused(
                operation.id, "its bounded retry budget is not spent"
            )
        if (
            operation.retry_disposition == _RETRY_DISPOSITION
            and operation.retry_disposition_attempt == operation.current_attempt
            and operation.retry_due_at is not None
        ):
            raise OperatorRetirementRefused(
                operation.id, "another attempt is already authorised"
            )
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        if (
            attempt is not None
            and attempt.state == "running"
            and _aware(attempt.lease_deadline) > _aware(now)
        ):
            raise OperatorRetirementRefused(
                operation.id, "a live attempt still holds its lease"
            )
        if _attempt_holds_open_launch_budget(operation, attempt, now):
            raise OperatorRetirementRefused(
                operation.id, "its issued start still holds an open launch budget"
            )
        if attempt is not None:
            attempts.append(attempt)
    reasons = {
        operation.id: (
            f"operator retired the parked {operation.kind} operation after its "
            f"{policy.max_failures}-attempt retry budget was spent; capacity "
            "is retained until exact cleanup confirms the effect stopped"
        )
        for operation in operations
    }
    for operation in operations:
        operation.state = "failed"
        operation.status_reason = reasons[operation.id][:512]
        operation.retry_disposition = None
        operation.retry_disposition_attempt = None
        operation.retry_due_at = None
        operation.updated_at = now
    for attempt in attempts:
        if attempt.state == "running":
            attempt.state = "expired"
    job_reason = reasons[operations[0].id]
    if job.kind in {
        "recipe.start",
        "recipe.stop",
        "recipe.install",
        "recipe.uninstall",
        "recipe.reconcile",
    }:
        previous = {} if job.result is None else job.result
        if job.result is not None:
            parse_recipe_lifecycle_result(job.kind, previous)
        # Reuse the lifecycle cancellation contract. Failed + cancelled is the
        # durable retirement handoff; ordinary cancellation uses cancelled state.
        job.result = RecipeOperationCancellationResult.model_validate_json(
            canonical_message(
                {
                    "cancel_requested": True,
                    "cancelled": True,
                    "cancel_requested_at": _aware(now).isoformat(),
                    "cancel_request_id": str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"vonk:retire:{job.id}")
                    ),
                    "cancel_actor": job.actor,
                    "reason": job_reason[:512],
                    **{
                        key: previous[key]
                        for key in ("node_evidence", "launch_evidence")
                        if key in previous
                    },
                }
            )
        ).model_dump(mode="json", exclude_none=True)
    _release_retired_owner_in_session(session, job, job_reason, now)
    job.state = "failed"
    job.status_reason = job_reason[:1024]
    job.updated_at = now
    return tuple(operation.id for operation in operations)


def _release_retired_owner_in_session(
    session: Session, job: Job, reason: str, now: datetime
) -> None:
    """Retain uncertain effects for the normal exact cleanup lifecycle.

    The owner binding is read from the job's own payload, so this stays the
    same authority the operation was admitted under. Retirement proves only
    that the order ended; even failed owners can still have physical effects.
    Only an already stopped/uninstalled owner has evidence to release capacity.
    """

    payload = job.payload if isinstance(job.payload, Mapping) else {}
    owner_kind = payload.get("owner_kind")
    owner_id = payload.get("owner_id")
    if isinstance(owner_kind, str) and isinstance(owner_id, str):
        if owner_kind == "run":
            run = session.get(RecipeRun, owner_id, with_for_update=True)
            if run is not None and run.state != "stopped":
                run.state = "lost"
                run.route_state = "withdrawn"
                run.route_error = reason[:512]
                run.updated_at = now
            elif run is not None:
                release_owned_reservations_in_session(
                    session, owner_kind, owner_id, now
                )
        elif owner_kind == "installation":
            installation = session.get(
                RecipeInstallation, owner_id, with_for_update=True
            )
            if installation is not None and installation.state != "uninstalled":
                installation.state = "partial"
                installation.updated_at = now
            elif installation is not None:
                release_owned_reservations_in_session(
                    session, owner_kind, owner_id, now
                )
    for child in session.scalars(
        select(StoredOperation).where(
            StoredOperation.parent_job_id == job.id,
            StoredOperation.state == "queued",
            StoredOperation.current_attempt == 0,
        )
    ):
        child.state = "cancelled"
        child.status_reason = "parent operation was retired by the operator"[:512]
        child.updated_at = now


def _failure_result(
    error_code: str, reason: str, *, uncertain: bool
) -> dict[str, object]:
    """Build a typed failure result through the redaction boundary."""

    evidence: dict[str, object] = {
        "error_code": error_code,
        "summary": reason,
        "reason": reason,
        "uncertain": uncertain,
        "recovery": "inspect-before-resume" if uncertain else "retry-or-inspect",
        "failure_kind": (
            FailureKind.UNCERTAIN_EFFECT.value
            if uncertain
            else FailureKind.INVALID_CONTRACT.value
        ),
    }
    if not uncertain:
        evidence["status"] = "failed"
    return sanitize_failure_evidence(evidence)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _lease_expiry_reason(
    operation: StoredOperation,
    previous: AgentOperationAttempt | None,
    node: AgentNode,
    now: datetime,
) -> str:
    """Return one durable reason for an attempt that stopped renewing.

    The lease deadline, the last accepted contact and the expiry instant are
    the facts that separate "the agent went away mid-effect" from "the agent is
    still working".  A late result can never be applied to an expired attempt,
    so these facts are what an operator or a recovery owner has to reconcile
    against the actual effect.
    """

    parts = [f"attempt {max(1, operation.current_attempt)} lease expired"]
    if previous is not None:
        parts.append(f"lease deadline {_aware(previous.lease_deadline).isoformat()}")
    parts.append(
        "last accepted contact never observed"
        if node.last_seen_at is None
        else f"last accepted contact {_aware(node.last_seen_at).isoformat()}"
    )
    parts.append(f"expired at {_aware(now).isoformat()}")
    return ("; ".join(parts) + "; the effect is unobserved")[:512]


def _operation_start_deadline(operation: StoredOperation) -> datetime | None:
    """Return the immutable start deadline a two-phase start bound, if any.

    Only a distributed start persists one, and it is the budget the start may not
    outlive.  It is the one clock that can bound a lapsed renewal: the lease is
    the thing being recovered, so it cannot also be the recovery budget.  An
    operation that binds none gets no allowance, because inventing a second clock
    would widen the fence without a fact to bound it.
    """

    payload = operation.payload if isinstance(operation.payload, Mapping) else {}
    value = payload.get("start_deadline")
    if not isinstance(value, str):
        return None
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError:
        return None
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        return None
    return deadline


def _lapsed_renewal_allowed(operation: StoredOperation, now: datetime) -> bool:
    """Return whether a lapsed lease may still be re-acquired by its own fence.

    A lease lapse parks a healthy start when the Controller was briefly
    unreachable, and the operation's own start budget is the clock that says how
    long that start is still legitimate.  Until it is spent, the executor that
    holds the fence may prove it is alive again; after it, nothing may.
    """

    deadline = _operation_start_deadline(operation)
    return deadline is not None and _aware(now) < _aware(deadline)


def _attempt_holds_open_launch_budget(
    operation: StoredOperation,
    attempt: AgentOperationAttempt | None,
    now: datetime,
) -> bool:
    """Return whether this exact attempt still owns an open launch budget.

    Dependency: the attempt is the executor of a start that declared an
    immutable readiness budget.  Owner: the operation's current attempt, still
    running.  Deadline: that budget's instant, which derives from the plan's own
    readiness window and never from the lease being recovered.  Resume
    condition: the exact fence submits its outcome, or the budget elapses.

    A start may legitimately be silent for the whole budget while its engine
    loads -- the recipe itself declares that window -- so the budget, not the
    lease the agent happened to accept, decides whether the attempt is still
    current.  The ownership facts are named here so every caller agrees on what
    "still current" means: only the operation's own attempt may use the
    allowance, so a newer attempt's takeover is never masked.
    """

    return (
        attempt is not None
        and attempt.attempt == operation.current_attempt
        and attempt.state == "running"
        and _lapsed_renewal_allowed(operation, now)
    )


def _attempt_is_live(
    operation: StoredOperation,
    attempt: AgentOperationAttempt | None,
    now: datetime,
) -> bool:
    """Return whether an attempt still holds the operation's fence.

    This is the boundary that decides whether another owner may take the
    operation over, and which attempts count as expired, so a missing attempt, an
    attempt that already stopped, or a lease deadline at or before ``now`` is not
    live.  A still-current attempt inside its own launch budget is live as well,
    because the budget is the plan's declared readiness window and the lease
    exists to bound takeover latency, not to end a launch the plan permitted.
    It is deliberately not the renewal boundary: ``_active`` additionally lets
    this exact fence renew inside the operation's own start allowance, which
    restores a healthy attempt without ever letting a second owner in.
    """

    return (
        attempt is not None
        and attempt.state == "running"
        and (
            _aware(attempt.lease_deadline) > _aware(now)
            or _attempt_holds_open_launch_budget(operation, attempt, now)
        )
    )


def _reconciled_dead_attempt_reason(
    operation: StoredOperation,
    attempt: AgentOperationAttempt | None,
    node: AgentNode,
    now: datetime,
) -> str:
    """Name why a non-terminal order no longer has an executor.

    A missing or already-stopped attempt is a different defect from an expired
    lease, so the operator surface keeps them apart instead of claiming a lease
    expired when none was ever recorded.
    """

    if attempt is None:
        detail = f"attempt {max(1, operation.current_attempt)} has no recorded attempt"
    elif attempt.state != "running":
        detail = f"attempt {operation.current_attempt} stopped in state {attempt.state}"
    else:
        return _lease_expiry_reason(operation, attempt, node, now)
    contact = (
        "last accepted contact never observed"
        if node.last_seen_at is None
        else f"last accepted contact {_aware(node.last_seen_at).isoformat()}"
    )
    return (
        f"{detail}; {contact}; reconciled at {_aware(now).isoformat()}; "
        "the effect is unobserved"
    )[:512]


#: Every recorded boundary refusal starts with one of these prefixes.  They keep
#: the structured reason recognisable, and they let a recorder replace an earlier
#: refusal on the parent job without overwriting an unrelated domain reason.
_CLAIM_REFUSAL_PREFIX = "claim refused: "
#: An admitted claim whose parent cancellation document is malformed is
#: recorded with this prefix.  It is a note, not a refusal: the claim is not
#: blocked, but the malformation is named rather than silently reading a
#: non-boolean value as the default ``false``.
_CLAIM_NOTE_PREFIX = "claim note: "
#: The heartbeat and result boundaries persist their own refusal notes so a
#: stalled operation is not left with no reason at all.
_BOUNDARY_REFUSAL_PREFIXES = ("heartbeat refused: ", "result refused: ")
_REFUSAL_PREFIXES = (
    _CLAIM_REFUSAL_PREFIX,
    _CLAIM_NOTE_PREFIX,
    *_BOUNDARY_REFUSAL_PREFIXES,
)
_MAX_CLAIM_REFUSAL_REASON = 512


def _is_refusal_reason(reason: str | None) -> bool:
    return reason is None or reason.startswith(_REFUSAL_PREFIXES)


def _refusal_reason(prefix: str, check: str, **facts: object) -> str:
    """Render one bounded, redacted control-plane refusal note.

    Only bounded control-plane facts belong here: the name of the refusing
    check, the operation kind and state, attempt and ordinal integers, and
    small state labels.  Payloads, digests, certificate material and
    agent-supplied strings must never be passed.  The result is redacted and
    truncated to the persisted ``status_reason`` width so a refusal cannot
    smuggle unbounded or sensitive data onto the operator surface.
    """

    rendered = "; ".join(
        f"{key}={value}" for key, value in facts.items() if value is not None
    )
    reason = prefix + check + (f" ({rendered})" if rendered else "")
    return redact_text(reason)[:_MAX_CLAIM_REFUSAL_REASON]


def _claim_refusal_reason(check: str, **facts: object) -> str:
    return _refusal_reason(_CLAIM_REFUSAL_PREFIX, check, **facts)


def _claim_note_reason(check: str, **facts: object) -> str:
    return _refusal_reason(_CLAIM_NOTE_PREFIX, check, **facts)


def _document(value: Mapping[str, object]) -> dict[str, object]:
    """Return the protocol's validated, deterministic JSON representation."""
    return json.loads(canonical_message(value))


def _signer_message(value: Mapping[str, object]) -> bytes:
    """Return the signer's canonical newline-delimited wire representation."""
    return canonical_message(value) + b"\n"


def _json_flag_parts(element: FunctionElement, compiler, **kwargs) -> tuple[str, str]:
    column, key = list(element.clauses)
    column_sql = compiler.process(column, **kwargs)
    key_sql = compiler.process(key, **kwargs)
    return column_sql, key_sql


class _JsonFlagIsTrue(FunctionElement[bool]):
    """True only when a JSON member is exactly the boolean ``true``.

    The predicate must not accept a SQL-coerced truthy value: the canonical
    lifecycle result declares ``cancel_requested: Literal[True]``, so a stored
    ``1`` or ``"true"`` is malformed and must not silently mean "cancelled".
    This is a JSON-level read, never a boolean cast.
    """

    type = Boolean()
    inherit_cache = True


@compiles(_JsonFlagIsTrue, "postgresql")
def _compile_json_flag_is_true_postgresql(element, compiler, **kwargs) -> str:
    column, key = _json_flag_parts(element, compiler, **kwargs)
    return (
        f"COALESCE(json_typeof({column} -> {key}) = 'boolean' "
        f"AND ({column} ->> {key}) = 'true', false)"
    )


@compiles(_JsonFlagIsTrue, "sqlite")
def _compile_json_flag_is_true_sqlite(element, compiler, **kwargs) -> str:
    column, key = _json_flag_parts(element, compiler, **kwargs)
    return f"COALESCE(json_type({column}, '$.' || {key}) = 'true', 0)"


@compiles(_JsonFlagIsTrue)
def _compile_json_flag_is_true_unsupported(element, compiler, **kwargs) -> str:
    raise NotImplementedError(
        "an exact JSON boolean read is not defined for this dialect"
    )


class _JsonFlagIsBoolean(FunctionElement[bool]):
    """True when a JSON member is absent or is a JSON boolean.

    False means the persisted value is present but malformed for a
    ``Literal[True]`` field.  It is a diagnostic only: the claim predicate
    reads it strictly and never blocks on it.
    """

    type = Boolean()
    inherit_cache = True


@compiles(_JsonFlagIsBoolean, "postgresql")
def _compile_json_flag_is_boolean_postgresql(element, compiler, **kwargs) -> str:
    column, key = _json_flag_parts(element, compiler, **kwargs)
    return (
        f"COALESCE(json_typeof({column} -> {key}) IS NULL "
        f"OR json_typeof({column} -> {key}) = 'boolean', true)"
    )


@compiles(_JsonFlagIsBoolean, "sqlite")
def _compile_json_flag_is_boolean_sqlite(element, compiler, **kwargs) -> str:
    column, key = _json_flag_parts(element, compiler, **kwargs)
    return (
        f"COALESCE(json_type({column}, '$.' || {key}) IS NULL "
        f"OR json_type({column}, '$.' || {key}) IN ('true', 'false'), 1)"
    )


@compiles(_JsonFlagIsBoolean)
def _compile_json_flag_is_boolean_unsupported(element, compiler, **kwargs) -> str:
    raise NotImplementedError(
        "an exact JSON boolean read is not defined for this dialect"
    )


@dataclass(frozen=True, slots=True)
class _ClaimCondition:
    """One named conjunct of the authoritative claimability predicate.

    ``expression`` is the single owner of the condition.  The claim query ANDs
    these objects together and the refusal classifier evaluates the very same
    objects one at a time, so a condition cannot exist without a name and an
    explanation cannot drift into a narrower re-implementation of the
    decision.  ``check`` is the operator-facing reason string rendered when the
    condition is the first one that does not hold.
    """

    check: str
    expression: ColumnElement[bool]


@dataclass(frozen=True, slots=True)
class _ClaimBranch:
    """The claimability conditions that apply to one non-terminal state."""

    state: str
    conditions: tuple[_ClaimCondition, ...]

    @property
    def expression(self) -> ColumnElement[bool]:
        return and_(
            StoredOperation.state == self.state,
            *(condition.expression for condition in self.conditions),
        )


@dataclass(frozen=True, slots=True)
class _ClaimPredicate:
    """The authoritative claimability predicate, decomposed by named condition.

    ``common`` holds the conditions every claimable operation satisfies;
    ``branches`` holds the state-specific ones, of which exactly one applies to
    any non-terminal operation.  ``diagnostics`` are named conditions that do
    not decide claimability but must still be visible when the refusal path
    explains an operation; keeping them beside the decision is what stops a
    malformed persisted value from becoming a silent default.
    """

    common: tuple[_ClaimCondition, ...]
    branches: tuple[_ClaimBranch, ...]
    diagnostics: tuple[_ClaimCondition, ...] = ()

    @property
    def expression(self) -> ColumnElement[bool]:
        return and_(
            *(condition.expression for condition in self.common),
            or_(*(branch.expression for branch in self.branches)),
        )

    def branch_for(self, state: str) -> _ClaimBranch | None:
        for branch in self.branches:
            if branch.state == state:
                return branch
        return None


def _claim_predicate(now: datetime) -> _ClaimPredicate:
    """Build the one authoritative predicate deciding what this node may claim.

    Every condition the predicate can fail is named here.  ``_claimable_operations``
    evaluates the whole conjunction; ``_excluded_work_refusal`` evaluates the
    same named conditions for one operation, so a reason cannot diverge from the
    decision it explains.
    """

    attempt_present = (
        select(AgentOperationAttempt.id)
        .where(
            AgentOperationAttempt.operation_id == StoredOperation.id,
            AgentOperationAttempt.attempt == StoredOperation.current_attempt,
        )
        .exists()
    )
    attempt_running = (
        select(AgentOperationAttempt.id)
        .where(
            AgentOperationAttempt.operation_id == StoredOperation.id,
            AgentOperationAttempt.attempt == StoredOperation.current_attempt,
            AgentOperationAttempt.state == "running",
        )
        .exists()
    )
    attempt_lease_elapsed = (
        select(AgentOperationAttempt.id)
        .where(
            AgentOperationAttempt.operation_id == StoredOperation.id,
            AgentOperationAttempt.attempt == StoredOperation.current_attempt,
            AgentOperationAttempt.lease_deadline <= now,
        )
        .exists()
    )
    # ``attempt_running`` and ``attempt_present`` are implied by
    # ``attempt_lease_elapsed``; they are separate named conditions only so the
    # refusal can tell a missing executor from a stopped one from a live lease.
    retry_ready_attempt = (
        select(AgentOperationAttempt.id)
        .where(
            AgentOperationAttempt.operation_id == StoredOperation.id,
            AgentOperationAttempt.attempt == StoredOperation.current_attempt,
            or_(
                AgentOperationAttempt.state.in_({"failed", "waiting-for-operator"}),
                and_(
                    AgentOperationAttempt.state == "expired",
                    AgentOperationAttempt.lease_deadline <= now,
                ),
            ),
        )
        .exists()
    )
    upgrade_safety_elapsed = (
        select(AgentOperationAttempt.id)
        .where(
            AgentOperationAttempt.operation_id == StoredOperation.id,
            AgentOperationAttempt.attempt == StoredOperation.current_attempt,
            AgentOperationAttempt.lease_deadline <= now,
        )
        .exists()
    )
    upgrade = AgentOperation.AGENT_UPGRADE.value
    return _ClaimPredicate(
        common=(
            _ClaimCondition("parent-job-missing", Job.id.is_not(None)),
            _ClaimCondition(
                "workload-intent-superseded",
                or_(
                    StoredOperation.workload_intent_ordinal.is_(None),
                    StoredOperation.workload_intent_ordinal
                    == AgentNode.workload_intent_ordinal,
                ),
            ),
            _ClaimCondition(
                "parent-cancel-requested",
                # The canonical lifecycle result declares
                # ``cancel_requested: Literal[True]``.  Read the JSON member
                # exactly, so a stored ``1`` or ``"true"`` is malformed rather
                # than a coercion of the SQL boolean type.
                _JsonFlagIsTrue(Job.result, "cancel_requested").is_not(True),
            ),
        ),
        branches=(
            _ClaimBranch(
                "queued",
                (
                    _ClaimCondition(
                        "queued-attempt-not-zero",
                        StoredOperation.current_attempt == 0,
                    ),
                ),
            ),
            _ClaimBranch(
                "running",
                (
                    _ClaimCondition("running-attempt-missing", attempt_present),
                    _ClaimCondition("running-attempt-not-running", attempt_running),
                    _ClaimCondition("running-lease-live", attempt_lease_elapsed),
                ),
            ),
            _ClaimBranch(
                "waiting-for-operator",
                (
                    _ClaimCondition(
                        "operator-retry-not-authorized",
                        and_(
                            StoredOperation.retry_disposition == _RETRY_DISPOSITION,
                            StoredOperation.retry_disposition_attempt
                            == StoredOperation.current_attempt,
                        ),
                    ),
                    # The upgrade timing gate and the ordinary retry-clock gate
                    # are mutually exclusive by kind; each is trivially true for
                    # the other kind, so ANDing them matches the predicate's
                    # kind-switched OR while still naming which one failed.
                    _ClaimCondition(
                        "upgrade-safety-not-elapsed",
                        or_(StoredOperation.kind != upgrade, upgrade_safety_elapsed),
                    ),
                    _ClaimCondition(
                        "operator-retry-not-due",
                        or_(
                            StoredOperation.kind == upgrade,
                            StoredOperation.retry_due_at.is_(None),
                            StoredOperation.retry_due_at <= now,
                        ),
                    ),
                    _ClaimCondition(
                        "operator-retry-attempt-not-ready", retry_ready_attempt
                    ),
                ),
            ),
        ),
        diagnostics=(
            # Not a claimability condition: a malformed flag must not block
            # legitimate work, but it must not be read as a silent ``false``
            # either.  The refusal path names it when it explains an operation.
            _ClaimCondition(
                "parent-cancel-flag-malformed",
                _JsonFlagIsBoolean(Job.result, "cancel_requested"),
            ),
        ),
    )


def _claim_condition_facts(
    check: str,
    operation: StoredOperation,
    attempt: AgentOperationAttempt | None,
    node: AgentNode,
    now: datetime,
) -> dict[str, object]:
    """Return the bounded facts that explain one failed named condition."""

    if check == "workload-intent-superseded":
        return {
            "operation_intent": operation.workload_intent_ordinal,
            "node_intent": node.workload_intent_ordinal,
        }
    if check in {
        "queued-attempt-not-zero",
        "operator-retry-not-authorized",
        "operator-retry-attempt-not-ready",
    }:
        return {"attempt": operation.current_attempt}
    if check in {"running-attempt-missing", "running-attempt-not-running"}:
        facts: dict[str, object] = {"attempt": operation.current_attempt}
        if attempt is not None:
            facts["attempt_state"] = attempt.state
        return facts
    if check in {"running-lease-live", "upgrade-safety-not-elapsed"}:
        facts = {"attempt": operation.current_attempt}
        if attempt is not None:
            facts["lease_deadline"] = _aware(attempt.lease_deadline).isoformat()
        return facts
    if check == "operator-retry-not-due":
        facts = {"attempt": operation.current_attempt}
        if operation.retry_due_at is not None:
            facts["retry_due_at"] = _aware(operation.retry_due_at).isoformat()
        return facts
    return {}


def _held_claim_conditions(
    session: Session,
    operation: StoredOperation,
    conditions: tuple[_ClaimCondition, ...],
) -> tuple[bool, ...]:
    """Evaluate each named claim condition for one operation, in order.

    The statement selects the predicate's own expressions, so the outcome is
    the decision itself rather than a second statement of it.  A SQL NULL is
    reported as "did not hold", matching the claim query's three-valued
    filtering.
    """

    statement = (
        select(
            *[
                condition.expression.label(f"claim_condition_{index}")
                for index, condition in enumerate(conditions)
            ]
        )
        .select_from(StoredOperation)
        .outerjoin(Job, Job.id == StoredOperation.parent_job_id)
        .outerjoin(AgentNode, AgentNode.node_id == StoredOperation.node_id)
        .where(StoredOperation.id == operation.id)
    )
    row = session.execute(statement).one()
    return tuple(bool(value) for value in row)


class AgentJobService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        result_consumer: ResultConsumer | None = None,
        contact_consumer: ContactConsumer | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if result_consumer is not None and not callable(result_consumer):
            raise TypeError("agent result consumer must be callable")
        if contact_consumer is not None and not callable(contact_consumer):
            raise TypeError("agent contact consumer must be callable")
        self._sessions = sessions
        self._clock = clock
        self._monotonic = monotonic
        self._result_consumer = result_consumer
        self._contact_consumer = contact_consumer
        self._configuration_lock = threading.Lock()
        self._started = False
        # SQLite ignores row locks. This only prevents same-service test races;
        # PostgreSQL correctness is provided by the database locks below.
        self._claim_lock = threading.RLock()
        self._available = threading.Condition()

    def enqueue(
        self,
        parent_job_id: str,
        node_id: str,
        operation: str,
        authority_revision: str,
        payload: Mapping[str, object],
    ) -> StoredOperation:
        with self._sessions.begin() as session:
            stored = self.enqueue_in_session(
                session,
                parent_job_id,
                node_id,
                operation,
                authority_revision,
                payload,
                operation_id=str(uuid.uuid4()),
            )
        self.notify_available()
        return stored

    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        authority_revision: str,
        payload: Mapping[str, object],
        *,
        operation_id: str,
    ) -> StoredOperation:
        """Attach a caller-identified operation to the caller's transaction."""
        self._mark_started()
        now = self._clock()
        try:
            protocol_operation = AgentOperation(operation)
        except ValueError as error:
            raise ValueError(
                "agent operation is not supported by the control plane"
            ) from error
        if protocol_operation.value not in _CONTROL_OPERATIONS:
            raise ValueError("agent operation is not supported by the control plane")
        targets = session.scalar(select(Job.targets).where(Job.id == parent_job_id))
        if targets is None:
            raise KeyError(parent_job_id)
        scope = self._target_scope(targets)
        if scope is None or node_id not in scope:
            raise ValueError("agent operation node must be a parent target")
        uses_workload_admission = protocol_operation.value in _RECIPE_CAPABILITIES
        try:
            if uses_workload_admission:
                acquire_admission_keys(
                    session, tuple(node_admission_key(target) for target in scope)
                )
            scopes_locked = self._lock_target_scopes(
                session,
                {"enqueue": (parent_job_id, scope)},
                node_id,
                nowait=uses_workload_admission,
            )
        except AdmissionLockBusy as error:
            if protocol_operation.value == AgentOperation.RECIPE_INSTALL.value:
                raise InstallAdmissionBusy("install.capacity_busy") from error
            if uses_workload_admission:
                raise RunAdmissionBusy("run capacity writer is busy") from error
            raise
        if not scopes_locked:
            raise ValueError("agent operation parent target scope changed")
        node = session.scalar(select(AgentNode).where(AgentNode.node_id == node_id))
        if node is None:
            raise KeyError(node_id)
        if node.state != "active" or node.revoked_at is not None:
            raise ValueError("agent operation node must be active")
        if node.capabilities and operation not in set(node.capabilities):
            raise ValueError(
                f"agent does not advertise operation capability {operation}"
            )
        parent = session.scalar(select(Job).where(Job.id == parent_job_id))
        if parent is None:
            raise KeyError(parent_job_id)
        if parent.state in _TERMINAL_PARENT_STATES:
            raise ValueError(
                "cannot enqueue an agent operation beneath a terminal parent"
            )
        if parent.authority_revision != authority_revision:
            raise ValueError("agent operation authority revision must match its parent")
        if node_id not in parent.targets:
            raise ValueError("agent operation node must be a parent target")
        workload_intent_ordinal = parent.payload.get("workload_intent_ordinal")
        if operation in _WORKLOAD_INTENT_OPERATIONS and workload_intent_ordinal is None:
            raise ValueError("workload operation requires a bound intent")
        if workload_intent_ordinal is not None and (
            type(workload_intent_ordinal) is not int
            or workload_intent_ordinal < 1
            or workload_intent_ordinal != node.workload_intent_ordinal
        ):
            raise ValueError("agent operation workload intent was superseded")
        reserved_fence = str(uuid.uuid4())
        payload_bytes = canonical_payload(protocol_operation, payload)
        final_payload = json.loads(payload_bytes)
        validated = AgentClaim(
            schema_version=1,
            job_id=parent_job_id,
            operation_id=operation_id,
            attempt=1,
            fence=reserved_fence,
            node_id=node_id,
            operation=protocol_operation,
            authority_revision=authority_revision,
            payload_digest=hashlib.sha256(payload_bytes).hexdigest(),
            payload=final_payload,
            deadline=now,
        )
        stored = StoredOperation(
            id=validated.operation_id,
            parent_job_id=parent_job_id,
            node_id=node_id,
            kind=protocol_operation.value,
            payload_digest=validated.payload_digest,
            payload=_document(validated.payload),
            authority_revision=authority_revision,
            workload_intent_ordinal=workload_intent_ordinal,
            state="queued",
            current_attempt=0,
            created_at=now,
            updated_at=now,
        )
        session.add(stored)
        session.flush()
        return stored

    def notify_available(self) -> None:
        """Wake long polls after a caller-managed enqueue transaction commits."""
        with self._available:
            self._available.notify_all()

    @staticmethod
    def request_superseded_workload_cancellation_in_session(
        session: Session, targets: Sequence[str], ordinal: int, now: datetime
    ) -> None:
        """Cancel older overlapping orders without declaring issued effects finished."""
        scope = tuple(sorted(set(targets)))
        if (
            not scope
            or len(scope) != len(targets)
            or type(ordinal) is not int
            or ordinal < 1
        ):
            raise ValueError("workload cancellation scope is invalid")
        acquire_admission_keys(
            session, tuple(node_admission_key(node_id) for node_id in scope)
        )
        parent_ids = tuple(
            session.scalars(
                select(StoredOperation.parent_job_id)
                .join(Job, Job.id == StoredOperation.parent_job_id)
                .where(
                    StoredOperation.node_id.in_(scope),
                    StoredOperation.kind.in_(_WORKLOAD_INTENT_OPERATIONS),
                    StoredOperation.workload_intent_ordinal.is_not(None),
                    StoredOperation.workload_intent_ordinal < ordinal,
                    Job.state.in_({"queued", "running", "waiting-for-operator"}),
                )
                .distinct()
                .order_by(StoredOperation.parent_job_id)
            )
        )
        locked = lock_admission_rows(
            session,
            (
                AdmissionRowLock(
                    "superseded-workload-parents",
                    Job,
                    select(Job).where(Job.id.in_(parent_ids)),
                ),
                AdmissionRowLock(
                    "superseded-workload-children",
                    StoredOperation,
                    select(StoredOperation).where(
                        StoredOperation.parent_job_id.in_(parent_ids)
                    ),
                ),
            )
            if parent_ids
            else (),
        )
        parents = {
            parent.id: parent
            for parent in locked.get("superseded-workload-parents", ())
        }
        children_by_parent: dict[str, list[StoredOperation]] = {}
        for child in locked.get("superseded-workload-children", ()):
            children_by_parent.setdefault(child.parent_job_id, []).append(child)
        for parent_id in parent_ids:
            parent = parents.get(parent_id)
            if parent is None or parent.state not in {
                "queued",
                "running",
                "waiting-for-operator",
            }:
                continue
            children = tuple(children_by_parent.get(parent_id, ()))
            bound = parent.payload.get("workload_intent_ordinal")
            if (
                type(bound) is not int
                or bound >= ordinal
                or not children
                or any(
                    child.kind not in _WORKLOAD_INTENT_OPERATIONS
                    or child.workload_intent_ordinal != bound
                    or child.node_id not in parent.targets
                    for child in children
                )
            ):
                raise ValueError("superseded workload order identity is invalid")
            for child in children:
                child.retry_disposition = None
                child.retry_disposition_attempt = None
                child.retry_due_at = None
                if child.state == "queued" and child.current_attempt == 0:
                    child.state = "cancelled"
                    child.status_reason = "superseded by newer workload intent"
                    child.updated_at = now
            if any(
                child.state in {"running", "waiting-for-operator"} for child in children
            ):
                previous = (
                    dict(parent.result) if isinstance(parent.result, Mapping) else {}
                )
                if previous.get("cancel_requested") is not True:
                    parent.result = {
                        **previous,
                        "cancel_requested": True,
                        "cancel_request_id": str(
                            uuid.uuid5(uuid.NAMESPACE_URL, f"{parent.id}:{ordinal}")
                        ),
                        "cancel_actor": "controller",
                        "cancel_requested_at": _aware(now).isoformat(),
                        "reason": "superseded by newer workload intent",
                    }
                elif superseded_cancellation_deadline(previous) is None:
                    # A repeated cancellation path must repair a result that
                    # carries the flag but no usable timestamp, not merely one
                    # with the key missing.  Without a parseable instant the
                    # cleanup STOP can never be authorised and the operation
                    # would wait forever.
                    parent.result = {
                        **previous,
                        "cancel_requested_at": _aware(now).isoformat(),
                    }
                parent.state = "running"
            elif all(
                child.state
                in {"succeeded", "failed", "cancelled", "waiting-for-operator"}
                for child in children
            ):
                # A superseded parent may have one rank finish just before the
                # replacement fences the other rank.  Once no child is still
                # running or parked, the cancellation is terminal even when
                # the children have mixed terminal outcomes.  Leaving the
                # parent ``running`` here makes every newer intent wait for a
                # receipt that can no longer arrive.
                parent.state = (
                    "failed"
                    if any(child.state == "failed" for child in children)
                    else "cancelled"
                )
            parent.status_reason = "superseded by newer workload intent"
            parent.updated_at = now

    @staticmethod
    def assess_superseded_agent_effects_in_session(
        session: Session, targets: Sequence[str], current_ordinal: int, now: datetime
    ) -> tuple[SupersededAgentEffect, ...]:
        """Read only: identify older issued effects still awaiting a stop receipt."""
        scope = tuple(sorted(set(targets)))
        if (
            not scope
            or len(scope) != len(targets)
            or type(current_ordinal) is not int
            or current_ordinal < 1
        ):
            raise ValueError("workload observation scope is invalid")
        candidates = tuple(
            session.scalars(
                select(StoredOperation)
                .where(
                    StoredOperation.node_id.in_(scope),
                    StoredOperation.kind.in_(_WORKLOAD_INTENT_OPERATIONS),
                    StoredOperation.workload_intent_ordinal.is_not(None),
                    StoredOperation.workload_intent_ordinal < current_ordinal,
                    StoredOperation.current_attempt > 0,
                    StoredOperation.state.in_({"running", "waiting-for-operator"}),
                )
                .order_by(StoredOperation.parent_job_id, StoredOperation.id)
            )
        )
        pending = []
        for operation in candidates:
            parent = session.get(Job, operation.parent_job_id)
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
            )
            deadline = superseded_cancellation_deadline(
                None if parent is None else parent.result
            )
            if (
                parent is None
                or attempt is None
                or deadline is None
                or operation.workload_intent_ordinal
                != parent.payload.get("workload_intent_ordinal")
                or operation.node_id not in parent.targets
                or AgentJobService._target_scope(parent.targets) is None
            ):
                raise ValueError("superseded agent effect identity is invalid")
            observation_deadline = max(
                deadline, _aware(attempt.lease_deadline)
            ) + timedelta(seconds=960)
            observe_due_at = min(
                observation_deadline,
                max(
                    _aware(now) + timedelta(seconds=2),
                    min(
                        _aware(attempt.lease_deadline),
                        _aware(now) + timedelta(seconds=30),
                    ),
                ),
            )
            pending.append(
                SupersededAgentEffect(
                    parent_job_id=parent.id,
                    operation_id=operation.id,
                    node_id=operation.node_id,
                    kind=operation.kind,
                    failure_kind=FailureKind.UNCERTAIN_EFFECT,
                    observe_due_at=observe_due_at,
                    observation_deadline=observation_deadline,
                )
            )
        return tuple(pending)

    def set_result_consumer(self, consumer: ResultConsumer) -> None:
        """Bind projection consumption once, before the queue serves any work."""
        if not callable(consumer):
            raise TypeError("agent result consumer must be callable")
        with self._configuration_lock:
            if self._result_consumer is not None:
                raise RuntimeError("agent result consumer is already configured")
            if self._started:
                raise RuntimeError("agent job service has already started")
            self._result_consumer = consumer

    def set_contact_consumer(self, consumer: ContactConsumer) -> None:
        """Bind atomic authenticated contact persistence before serving work."""

        if not callable(consumer):
            raise TypeError("agent contact consumer must be callable")
        with self._configuration_lock:
            if self._contact_consumer is not None:
                raise RuntimeError("agent contact consumer is already configured")
            if self._started:
                raise RuntimeError("agent job service has already started")
            self._contact_consumer = consumer

    def _mark_started(self) -> None:
        with self._configuration_lock:
            self._started = True

    def claim(
        self,
        node_id: str,
        certificate_serial: str,
        lease_seconds: int,
        wait_seconds: float = 0,
        protocol_version: int | None = 3,
        capabilities: Sequence[str] | None = tuple(_NEXT_CAPABILITIES),
        *,
        runtime_identity: Mapping[str, object] | None,
        hostname: str | None = None,
        source: AgentSource | None = None,
    ) -> AgentClaim | None:
        self._mark_started()
        if (
            not node_id.strip()
            or not certificate_serial.strip()
            or lease_seconds <= 0
            or isinstance(wait_seconds, bool)
            or not 0 <= wait_seconds <= 60
            or (
                hostname is not None
                and (
                    not isinstance(hostname, str)
                    or len(hostname) > 255
                    or re.fullmatch(
                        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                        r"(?:\.[A-Za-z0-9]"
                        r"(?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*",
                        hostname,
                    )
                    is None
                )
            )
            or (
                protocol_version is not None
                and (
                    isinstance(protocol_version, bool)
                    or not isinstance(protocol_version, int)
                    or not 1 <= protocol_version <= 2_147_483_647
                )
            )
        ):
            raise ValueError("node, certificate, and positive lease are required")
        advertised = self._capabilities(capabilities)
        running = self._runtime_identity(runtime_identity)
        deadline = self._monotonic() + wait_seconds
        with self._available:
            while True:
                claim = self._claim_once(
                    node_id,
                    certificate_serial,
                    lease_seconds,
                    protocol_version,
                    advertised,
                    running,
                    hostname,
                    source,
                )
                if claim is not None:
                    return claim
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return None
                # Let the agent drain this HTTP request and activate its staged
                # credential promptly, including with short-lived certificates.
                with self._sessions() as session:
                    now = self._clock()
                    staged = session.scalar(
                        select(AgentCertificate.serial)
                        .where(
                            AgentCertificate.node_id == node_id,
                            AgentCertificate.state == "staged",
                            AgentCertificate.revoked_at.is_(None),
                            AgentCertificate.ca_revoked_at.is_(None),
                            AgentCertificate.not_before <= now,
                            AgentCertificate.not_after > now,
                        )
                        .limit(1)
                    )
                if staged is not None:
                    return None
                self._available.wait(min(remaining, _DATABASE_REPOLL_SECONDS))

    def _record_claim_refusal(
        self,
        session: Session,
        *,
        operation: StoredOperation | None,
        job_id: str | None,
        check: str,
        **facts: object,
    ) -> None:
        """Persist a bounded reason for a refused claim without changing it.

        A refusal must never be silent: an operator has to be able to tell
        "there is no work" apart from "work exists and this check refused it".
        The reason is written to the operation, the durable per-operation
        surface already documented as "why this operation is not currently
        progressing", and to its parent job, the operator-facing surface the
        jobs API returns.  Writing only on change keeps a long-polling agent
        from turning one stuck operation into a write per poll.  The parent
        reason is only replaced when it is absent or was itself a refusal, so a
        domain reason such as "superseded by newer workload intent" is never
        overwritten.
        """

        self._write_refusal_note(
            session,
            operation=operation,
            job_id=job_id,
            reason=_claim_refusal_reason(check, **facts),
        )

    def _write_refusal_note(
        self,
        session: Session,
        *,
        operation: StoredOperation | None,
        job_id: str | None,
        reason: str,
    ) -> None:
        """Write one bounded refusal note without erasing a domain reason.

        A refusal explains work that will not proceed, so it may annotate only a
        target whose outcome is still open.  The owning state decides that, not
        the submission's correlating fence: a late but genuinely stale heartbeat
        or result still names the current attempt and fence of an operation that
        has already concluded, and writing the note then leaves the record
        carrying a refusal for a completed result.
        """

        if (
            operation is not None
            and operation.state not in _CONCLUDED_OUTCOMES
            and operation.status_reason != reason
            and _is_refusal_reason(operation.status_reason)
        ):
            # A domain reason such as "retry budget exhausted" already explains
            # why the operation is not progressing; a refusal must add evidence,
            # never erase it.
            operation.status_reason = reason
        if job_id is not None:
            job = session.get(Job, job_id)
            if (
                job is not None
                and job.state not in _CONCLUDED_OUTCOMES
                and job.status_reason != reason
                and _is_refusal_reason(job.status_reason)
            ):
                job.status_reason = reason

    def _note_malformed_cancel_flag(
        self, session: Session, operation: StoredOperation
    ) -> None:
        """Name a malformed persisted cancel flag without blocking the claim.

        ``_JsonFlagIsTrue`` reads the parent's ``cancel_requested`` exactly, so
        a stored ``1`` or ``"true"`` no longer cancels and legitimate work is
        not blocked.  Reading it silently would invent the default ``false``,
        which the persisted-contract rule forbids, so the malformation is named
        on the operator surface instead.  The refusal classifier cannot carry
        this case: it only runs when no operation is admitted, and this
        operation was just admitted.
        """

        job = session.get(Job, operation.parent_job_id)
        if job is None or not isinstance(job.result, Mapping):
            return
        if "cancel_requested" not in job.result:
            return
        if isinstance(job.result["cancel_requested"], bool):
            return
        self._write_refusal_note(
            session,
            operation=None,
            job_id=job.id,
            reason=_claim_note_reason(
                "parent-cancel-flag-malformed", kind=operation.kind
            ),
        )

    def record_boundary_refusal(
        self,
        operation_id: str,
        attempt: int,
        fence: str,
        *,
        boundary: str,
        check: str,
        **facts: object,
    ) -> bool:
        """Persist why a heartbeat or result failed at the Controller boundary.

        The claim path already explains its refusals, but a refused heartbeat or
        result persisted nothing, so an operator could see an operation that
        stopped progressing with no reason at all.  The note is written only
        when the submission names the operation's current attempt and fence, so
        a replayed old boundary cannot annotate newer work.  The parent job
        receives the same note on the same surface the jobs API returns.
        """

        prefixes = {
            "heartbeat": "heartbeat refused: ",
            "result": "result refused: ",
        }
        prefix = prefixes.get(boundary)
        if prefix is None:
            raise ValueError("agent boundary is invalid")
        reason = _refusal_reason(prefix, check, attempt=attempt, **facts)
        with self._sessions.begin() as session:
            operation = session.scalar(
                select(StoredOperation)
                .where(StoredOperation.id == operation_id)
                .with_for_update(of=StoredOperation)
            )
            if operation is None or operation.current_attempt != attempt:
                return False
            current = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation_id,
                    AgentOperationAttempt.attempt == attempt,
                )
            )
            if current is None or current.fence != fence:
                return False
            self._write_refusal_note(
                session,
                operation=operation,
                job_id=operation.parent_job_id,
                reason=reason,
            )
            return True

    def _excluded_work_refusal(
        self,
        session: Session,
        node: AgentNode,
        now: datetime,
    ) -> tuple[StoredOperation, str, dict[str, object]] | None:
        """Explain why a non-terminal operation on this node was not offered.

        ``_claimable_operations`` is a closed predicate.  When it matches
        nothing, the claim path cannot tell "no work exists" from "work exists
        and this node may not execute it now", which is exactly the silent
        wedge an operator cannot diagnose.  This read-only probe evaluates the
        predicate's own named conditions, in order, for the oldest non-terminal
        operation and reports the first that did not hold, so the explanation
        cannot become a narrower restatement of the decision.  It never grants
        or retries a claim.
        """

        operation = session.scalar(
            select(StoredOperation)
            .where(
                StoredOperation.node_id == node.node_id,
                StoredOperation.state.in_(
                    {"queued", "running", "waiting-for-operator"}
                ),
            )
            .order_by(StoredOperation.created_at, StoredOperation.id)
            .limit(1)
        )
        if operation is None:
            return None
        facts: dict[str, object] = {"kind": operation.kind, "state": operation.state}
        predicate = _claim_predicate(now)
        branch = predicate.branch_for(operation.state)
        # Diagnostics come first so a malformed persisted value is named before
        # the condition it may have masked.
        conditions = (
            predicate.diagnostics
            + predicate.common
            + (() if branch is None else branch.conditions)
        )
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        held = _held_claim_conditions(session, operation, conditions)
        for condition, condition_held in zip(conditions, held, strict=True):
            if condition_held:
                continue
            return (
                operation,
                condition.check,
                {
                    **facts,
                    **_claim_condition_facts(
                        condition.check, operation, attempt, node, now
                    ),
                },
            )
        # Every modelled condition held, yet the claim query matched nothing.
        # No unmodelled condition can exist - the predicate is built from the
        # conditions just evaluated - so this is a defensive last resort, not a
        # path any real operation takes.
        return (
            operation,
            "unclassified-unclaimable",
            {
                **facts,
                "attempt": operation.current_attempt,
            },
        )

    def _cancel_superseded_operation(
        self,
        session: Session,
        operation: StoredOperation,
        parent: Job,
        now: datetime,
        *,
        superseded_by: int | None,
        disarmed: bool,
    ) -> None:
        """Drive a superseded, non-terminal order to its known terminal state.

        A ``waiting-for-operator`` operation has no live attempt, so it can
        never deliver the cancellation receipt that a pending supersession
        waits for.  The *order* outcome is already known - cancelled - even if
        the effect is unobserved, so the operation becomes terminal instead of
        blocking later work forever.  The effect is never re-issued.  A
        cancellation recorded without a parseable ``cancel_requested_at`` is a
        defect that disarmed cleanup entirely, so the reason names it instead
        of silently disabling recovery.
        """

        detail = (
            "cancel_requested_at missing or unparseable"
            if disarmed
            else "cancellation cleanup deadline elapsed"
        )
        operation.state = "cancelled"
        operation.status_reason = (
            f"superseded by workload intent {superseded_by}; intent "
            f"{operation.workload_intent_ordinal} cancelled ({detail})"
        )[:512]
        operation.retry_disposition = None
        operation.retry_disposition_attempt = None
        operation.retry_due_at = None
        operation.updated_at = now
        # Aggregate first: it recomputes the parent's operator reason from its
        # children, so the defect note has to be written afterwards to survive.
        self._aggregate_parent(session, operation.parent_job_id)
        if disarmed and _is_refusal_reason(parent.status_reason):
            parent.status_reason = (
                "cancel_requested carried no cancel_requested_at; the superseded "
                "order was reconciled to cancelled"
            )[:1024]

    def _superseded_cancellation_state(
        self,
        session: Session,
        old: StoredOperation,
        operation: StoredOperation,
        now: datetime,
    ) -> str:
        """Classify a prior order against the current order's supersession.

        ``block`` means an authorised cleanup window is still live, so the prior
        order must finish before later work runs.  ``cancelled`` means the prior
        order was already driven to its known terminal state here.  Anything
        else is not a supersession and is decided by the caller.
        """

        if not (
            operation.kind in _WORKLOAD_INTENT_OPERATIONS
            and old.kind in _WORKLOAD_INTENT_OPERATIONS
            and old.current_attempt > 0
            and old.workload_intent_ordinal is not None
            and operation.workload_intent_ordinal is not None
            and old.workload_intent_ordinal < operation.workload_intent_ordinal
        ):
            return "not-applicable"
        old_parent = session.get(Job, old.parent_job_id)
        if not (
            old_parent is not None
            and isinstance(old_parent.result, Mapping)
            and old_parent.result.get("cancel_requested") is True
        ):
            return "not-applicable"
        cancellation_deadline = superseded_cancellation_deadline(old_parent.result)
        if cancellation_deadline is not None and _aware(now) < cancellation_deadline:
            return "block"
        self._cancel_superseded_operation(
            session,
            old,
            old_parent,
            now,
            superseded_by=operation.workload_intent_ordinal,
            disarmed=cancellation_deadline is None,
        )
        return "cancelled"

    def _reconcile_dead_running_operation(
        self,
        session: Session,
        operation: StoredOperation,
        attempt: AgentOperationAttempt | None,
        node: AgentNode,
        now: datetime,
        *,
        superseded_by: int | None,
    ) -> None:
        """Park a running order whose attempt can no longer report.

        The order's effect is unobserved, never proved ended, so it becomes a
        durable operator-visible wait instead of a permanent blocker.  The
        effect is never re-issued: only the existing exact-resume path may
        schedule a retry, and only for a restart-safe operation.  The recorded
        result and fence are retained so a late receipt is still accepted.
        """

        if attempt is not None and attempt.state in {
            "running",
            "waiting-for-operator",
        }:
            attempt.state = "expired"
        operation.state = "waiting-for-operator"
        reason = _reconciled_dead_attempt_reason(operation, attempt, node, now)
        if superseded_by is not None and operation.workload_intent_ordinal is not None:
            reason = f"superseded by workload intent {superseded_by}; {reason}"
        operation.retry_disposition = None
        operation.retry_disposition_attempt = None
        operation.retry_due_at = None
        if operation.kind in _RESTART_REISSUE_OPERATIONS:
            self._schedule_safe_retry(operation, now)
            scheduled = operation.status_reason
            if isinstance(scheduled, str) and scheduled:
                reason = f"{reason}; {scheduled}"
        # The reconciliation reason is the durable fact an operator needs, so
        # it survives the schedule note instead of being replaced by it.
        operation.status_reason = reason[:512]
        operation.updated_at = now
        self._project_artifact_job_expiry(session, operation, now)
        self._aggregate_parent(session, operation.parent_job_id)

    @staticmethod
    def _claimable_operations(
        node_id: str, now: datetime, capabilities: tuple[str, ...] | None
    ):
        supported = StoredOperation.kind.in_(capabilities or ())
        if "agent.lifecycle.resume.exact.v1" not in (capabilities or ()):
            supported = and_(
                supported,
                and_(
                    StoredOperation.kind.in_(_LIFECYCLE_RESTART_OPERATIONS),
                    StoredOperation.current_attempt > 0,
                    StoredOperation.retry_disposition == _RETRY_DISPOSITION,
                    StoredOperation.retry_disposition_attempt
                    == StoredOperation.current_attempt,
                ).is_not(True),
            )
        return (
            select(StoredOperation)
            .join(Job, Job.id == StoredOperation.parent_job_id)
            .join(AgentNode, AgentNode.node_id == StoredOperation.node_id)
            .where(
                StoredOperation.node_id == node_id,
                # The one owner of every claimability condition, shared with
                # ``_excluded_work_refusal`` so a refusal cannot restate (and
                # drift from) the decision it explains.
                _claim_predicate(now).expression,
            )
            # Choose work this agent can perform before limiting the queue.
            # Otherwise a retry requiring a newer agent starves its own upgrade.
            # Keep unsupported work as a fallback so the authority check can
            # still record its actionable reason when no supported work is due.
            .order_by(
                case((supported, 0), else_=1),
                StoredOperation.created_at,
                StoredOperation.id,
            )
            .execution_options(populate_existing=True)
            .limit(1)
        )

    def _claim_once(
        self,
        node_id: str,
        certificate_serial: str,
        lease_seconds: int,
        protocol_version: int | None,
        capabilities: tuple[str, ...] | None,
        runtime_identity: AgentRuntimeIdentity,
        hostname: str | None,
        source: AgentSource | None,
    ) -> AgentClaim | None:
        with self._claim_lock, self._sessions.begin() as session:
            now = self._clock()
            candidate_id = session.scalar(
                self._claimable_operations(
                    node_id, now, capabilities
                ).with_only_columns(StoredOperation.id)
            )
            recovery_id = None
            if candidate_id is None:
                predicate = _claim_predicate(now)
                for parked, attempt in session.execute(
                    select(StoredOperation, AgentOperationAttempt)
                    .join(Job, Job.id == StoredOperation.parent_job_id)
                    .join(AgentNode, AgentNode.node_id == StoredOperation.node_id)
                    .join(
                        AgentOperationAttempt,
                        and_(
                            AgentOperationAttempt.operation_id == StoredOperation.id,
                            AgentOperationAttempt.attempt
                            == StoredOperation.current_attempt,
                        ),
                    )
                    .where(
                        StoredOperation.node_id == node_id,
                        StoredOperation.state == "waiting-for-operator",
                        StoredOperation.kind.in_(_RESTART_REISSUE_OPERATIONS),
                        StoredOperation.retry_disposition.is_(None),
                        StoredOperation.retry_due_at.is_(None),
                        Job.state.in_({"queued", "running", "waiting-for-operator"}),
                        *(condition.expression for condition in predicate.common),
                        *(condition.expression for condition in predicate.diagnostics),
                    )
                    .order_by(StoredOperation.created_at, StoredOperation.id)
                ):
                    if _parked_retry_evidence(parked, attempt, now):
                        recovery_id = parked.id
                        break
            upgrade_id = None
            if (
                capabilities is not None
                and AgentOperation.AGENT_UPGRADE.value in capabilities
                and runtime_identity.package_activation is not None
            ):
                receipt = runtime_identity.package_activation
                upgrade_id = session.scalar(
                    select(StoredOperation.id)
                    .where(
                        StoredOperation.node_id == node_id,
                        StoredOperation.kind == AgentOperation.AGENT_UPGRADE.value,
                        StoredOperation.payload["rollback"]["attempt_nonce"].as_string()
                        == receipt.attempt_nonce,
                        StoredOperation.state.in_(
                            {"queued", "running", "waiting-for-operator"}
                        ),
                    )
                    .order_by(StoredOperation.created_at, StoredOperation.id)
                    .limit(1)
                )
            scopes = self._lock_operation_scopes(
                session,
                tuple(
                    dict.fromkeys(
                        value
                        for value in (candidate_id, upgrade_id, recovery_id)
                        if value is not None
                    )
                ),
                node_id,
            )
            if scopes is None:
                refused_id = candidate_id if candidate_id is not None else upgrade_id
                if refused_id is not None:
                    self._record_claim_refusal(
                        session,
                        operation=session.get(StoredOperation, refused_id),
                        job_id=None,
                        check="operation-scope-lock",
                    )
                return None
            identity = self._lock_identity(session, node_id, certificate_serial)
            now = self._clock()
            if identity is None or not self._identity_is_active(*identity, now):
                if candidate_id is not None:
                    self._record_claim_refusal(
                        session,
                        operation=session.get(StoredOperation, candidate_id),
                        job_id=None,
                        check="node-identity-inactive",
                    )
                return None
            node, certificate = identity
            self._validate_agent_contract(
                protocol_version, capabilities, runtime_identity
            )
            self._consume_contact(session, source, node, certificate)
            self._record_contact(
                session,
                node,
                certificate,
                now,
                protocol_version,
                capabilities,
                runtime_identity,
                hostname,
            )
            self._reconcile_agent_upgrade(
                session,
                node_id,
                certificate.serial,
                now,
                capabilities,
                runtime_identity,
                operation_id=upgrade_id,
                parent_job_id=None if upgrade_id is None else scopes[upgrade_id][0],
            )
            if recovery_id is not None:
                parked = session.scalar(
                    select(StoredOperation)
                    .where(StoredOperation.id == recovery_id)
                    .with_for_update(of=StoredOperation)
                    .execution_options(populate_existing=True)
                )
                attempt = (
                    session.scalar(
                        select(AgentOperationAttempt)
                        .where(
                            AgentOperationAttempt.operation_id == recovery_id,
                            AgentOperationAttempt.attempt == parked.current_attempt,
                        )
                        .with_for_update(of=AgentOperationAttempt)
                    )
                    if parked is not None
                    else None
                )
                if (
                    parked is not None
                    and attempt is not None
                    and _parked_retry_evidence(parked, attempt, now)
                ):
                    retry_after = (
                        attempt.result.get("retry_after_seconds")
                        if isinstance(attempt.result, Mapping)
                        else None
                    )
                    previous_reason = parked.status_reason
                    self._schedule_safe_retry(
                        parked, now, retry_after if type(retry_after) is int else None
                    )
                    if self._claim_has_authority(
                        session,
                        parked,
                        now,
                        node=node,
                        protocol_version=protocol_version,
                        capabilities=capabilities,
                        locked_targets=scopes[recovery_id][1],
                    ):
                        parked.updated_at = now
                        self._aggregate_parent(session, parked.parent_job_id)
                    else:
                        parked.retry_disposition = None
                        parked.retry_disposition_attempt = None
                        parked.retry_due_at = None
                        parked.status_reason = previous_reason
                # Recovery schedules a future exact claim. It never reissues
                # an effect inside this observation of an old parked attempt.
                return None
            if candidate_id is None:
                excluded = self._excluded_work_refusal(session, node, now)
                if excluded is not None:
                    excluded_operation, refusal_check, refusal_facts = excluded
                    self._record_claim_refusal(
                        session,
                        operation=excluded_operation,
                        job_id=excluded_operation.parent_job_id,
                        check=refusal_check,
                        **refusal_facts,
                    )
                return None
            statement = (
                self._claimable_operations(node_id, now, capabilities)
                .where(StoredOperation.id == candidate_id)
                .with_for_update(of=StoredOperation, skip_locked=True)
                .execution_options(populate_existing=True)
            )
            operation = session.scalar(statement)
            if operation is None or operation.parent_job_id != scopes[candidate_id][0]:
                if operation is not None:
                    self._record_claim_refusal(
                        session,
                        operation=operation,
                        job_id=operation.parent_job_id,
                        check="parent-scope-changed",
                        kind=operation.kind,
                    )
                return None
            if not self._claim_has_authority(
                session,
                operation,
                now,
                node=node,
                protocol_version=protocol_version,
                capabilities=capabilities,
                locked_targets=scopes[candidate_id][1],
            ):
                return None
            if capabilities is not None and operation.kind not in capabilities:
                self._record_claim_refusal(
                    session,
                    operation=operation,
                    job_id=operation.parent_job_id,
                    check="capability-unadvertised",
                    kind=operation.kind,
                )
                return None
            if operation.kind in _RECIPE_CAPABILITIES and (
                protocol_version != 3 or capabilities is None
            ):
                self._record_claim_refusal(
                    session,
                    operation=operation,
                    job_id=operation.parent_job_id,
                    check="recipe-protocol-unsupported",
                    kind=operation.kind,
                    protocol_version=protocol_version,
                )
                return None
            if (
                operation.kind == AgentOperation.RECIPE_BUILD.value
                and not self._recipe_build_runtime_matches(
                    session, operation, runtime_identity
                )
            ):
                self._reject_recipe_build_claim(
                    session, operation, certificate_serial, now
                )
                self._record_claim_refusal(
                    session,
                    operation=operation,
                    job_id=operation.parent_job_id,
                    check="builder-runtime-changed",
                    kind=operation.kind,
                )
                return None
            if operation.kind in _MUTATING_OPERATIONS:
                candidates = tuple(
                    session.scalars(
                        select(StoredOperation)
                        .where(
                            StoredOperation.node_id == node_id,
                            StoredOperation.id != operation.id,
                            StoredOperation.kind.in_(_MUTATING_OPERATIONS),
                            StoredOperation.state.in_(
                                {"running", "waiting-for-operator"}
                            ),
                        )
                        .order_by(StoredOperation.id)
                        .with_for_update(of=StoredOperation)
                    )
                )
                active_mutations_list = []
                reconciled_dead_running = False
                for old in candidates:
                    if old.state == "running":
                        attempt = session.scalar(
                            select(AgentOperationAttempt)
                            .where(
                                AgentOperationAttempt.operation_id == old.id,
                                AgentOperationAttempt.attempt == old.current_attempt,
                            )
                            .with_for_update(of=AgentOperationAttempt)
                        )
                        if _attempt_is_live(old, attempt, now):
                            # A live lease -- or a still-current attempt inside
                            # its own launch budget -- can still renew and report
                            # its exact effect; nothing may overlap it.
                            active_mutations_list.append(old)
                            continue
                        # The order is `running` but its attempt can no longer
                        # renew or report.  It can never deliver the receipt a
                        # wait needs, so leaving it as a blocker wedges every
                        # later mutation on this node forever (#811).  A
                        # superseded cancellation is reconciled first; anything
                        # else becomes a durable operator-visible wait.  The
                        # effect is never re-issued.
                        supersession = self._superseded_cancellation_state(
                            session, old, operation, now
                        )
                        if supersession == "block":
                            active_mutations_list.append(old)
                            continue
                        if supersession == "cancelled":
                            continue
                        self._reconcile_dead_running_operation(
                            session,
                            old,
                            attempt,
                            node,
                            now,
                            superseded_by=(
                                operation.workload_intent_ordinal
                                if (
                                    old.workload_intent_ordinal is not None
                                    and operation.workload_intent_ordinal is not None
                                    and old.workload_intent_ordinal
                                    < operation.workload_intent_ordinal
                                )
                                else None
                            ),
                        )
                        reconciled_dead_running = True
                        continue
                    # A waiting-for-operator order is decided by the
                    # supersession state alone: #810 reconciles an authorised
                    # cancellation that can no longer arrive, and a plain wait
                    # never blocks later work.
                    if (
                        self._superseded_cancellation_state(
                            session, old, operation, now
                        )
                        == "block"
                    ):
                        active_mutations_list.append(old)
                active_mutations = tuple(active_mutations_list)
                if reconciled_dead_running and not active_mutations:
                    self._record_claim_refusal(
                        session,
                        operation=operation,
                        job_id=operation.parent_job_id,
                        check="dead-mutation-reconciled",
                        kind=operation.kind,
                        operation_intent=operation.workload_intent_ordinal,
                    )
                    return None
                # A current exact STOP is the cleanup action for an older
                # cancelled workload. Do not let the old order's bookkeeping
                # prevent that STOP from reaching the agent; every other
                # mutation still waits for its prior effect to cease.
                current_ordinal = operation.workload_intent_ordinal
                stop_cleans_superseded = (
                    operation.kind == AgentOperation.RECIPE_STOP.value
                    and current_ordinal is not None
                )
                if (
                    operation.kind == AgentOperation.RECIPE_STOP.value
                    and current_ordinal is not None
                ):
                    for old in active_mutations:
                        old_parent = session.get(Job, old.parent_job_id)
                        if (
                            old.kind
                            not in {
                                AgentOperation.RECIPE_START.value,
                                AgentOperation.RECIPE_STOP.value,
                            }
                            or old.workload_intent_ordinal is None
                            or old.workload_intent_ordinal >= current_ordinal
                            or old.payload.get("run_id")
                            != operation.payload.get("run_id")
                            or old_parent is None
                            or not isinstance(old_parent.result, Mapping)
                            or old_parent.result.get("cancel_requested") is not True
                        ):
                            stop_cleans_superseded = False
                            break
                if active_mutations and not stop_cleans_superseded:
                    blocking = active_mutations[0]
                    self._record_claim_refusal(
                        session,
                        operation=operation,
                        job_id=operation.parent_job_id,
                        check="live-mutation-in-progress",
                        kind=operation.kind,
                        operation_intent=operation.workload_intent_ordinal,
                        blocking_kind=blocking.kind,
                        blocking_state=blocking.state,
                        blocking_operation=blocking.id,
                        blocking_intent=blocking.workload_intent_ordinal,
                    )
                    return None
            resumable_progress = None
            if operation.current_attempt:
                previous = session.scalar(
                    select(AgentOperationAttempt)
                    .where(
                        AgentOperationAttempt.operation_id == operation.id,
                        AgentOperationAttempt.attempt == operation.current_attempt,
                    )
                    .with_for_update(of=AgentOperationAttempt)
                )
                if (
                    previous is not None
                    and operation.kind == AgentOperation.ARTIFACT_DISTRIBUTION.value
                ):
                    resumable_progress = (
                        None
                        if previous.progress is None
                        else validate_progress_update(None, previous.progress)
                    )
                if previous is not None and previous.state in {
                    "running",
                    "waiting-for-operator",
                }:
                    if operation.state == "running" and (
                        _attempt_holds_open_launch_budget(operation, previous, now)
                    ):
                        # The attempt is the launch its own immutable budget
                        # declares live.  A poll for work cannot expire or park
                        # it, and no new attempt may be issued over it; the exact
                        # fence keeps the operation until it reports or the
                        # budget elapses.
                        return None
                    previous.state = "expired"
            if operation.state == "running":
                operation.state = "waiting-for-operator"
                # An expiry parks the operation without an attempt result, so
                # record why it stopped and the last facts describing the
                # interruption.  Without this an uncertain operation carries no
                # evidence at all and no operator can tell a lost connection
                # from an effect that may already have happened.
                operation.status_reason = _lease_expiry_reason(
                    operation, previous, node, now
                )
                operation.retry_disposition = None
                operation.retry_disposition_attempt = None
                operation.retry_due_at = None
                if operation.kind in _RESTART_REISSUE_OPERATIONS:
                    self._schedule_safe_retry(operation, now)
                operation.updated_at = now
                self._project_artifact_job_expiry(session, operation, now)
                self._aggregate_parent(session, operation.parent_job_id)
                return None
            if operation.kind == AgentOperation.AGENT_UPGRADE.value:
                import secrets

                from vonk_agent_protocol.contracts import AgentUpgradePayload

                payload = AgentUpgradePayload.model_validate(operation.payload)
                if (
                    runtime_identity.binary_digest
                    != payload.rollback.source.binary_sha256
                ):
                    return None
                # A new claim owns a fresh watchdog authority. The retry query
                # already enforced the full previous rollback safety fence.
                document = payload.model_dump(mode="json")
                document["rollback"].update(
                    attempt_nonce=secrets.token_hex(32),
                    activation_deadline=int(now.timestamp()) + 900,
                )
                payload_bytes = canonical_payload(
                    AgentOperation.AGENT_UPGRADE, document
                )
                operation.payload = json.loads(payload_bytes)
                operation.payload_digest = hashlib.sha256(payload_bytes).hexdigest()
            operation.current_attempt += 1
            operation.state = "running"
            operation.retry_due_at = None
            # A live attempt has no interrupted reason: keeping the previous
            # one would describe work that is running again.
            operation.status_reason = None
            operation.updated_at = now
            fence = str(uuid.uuid4())
            deadline = now + timedelta(seconds=lease_seconds)
            attempt = AgentOperationAttempt(
                operation_id=operation.id,
                attempt=operation.current_attempt,
                fence=fence,
                lease_deadline=deadline,
                agent_certificate_serial=certificate_serial,
                state="running",
                progress=resumable_progress,
            )
            session.add(attempt)
            self._note_malformed_cancel_flag(session, operation)
            return AgentClaim.model_validate(
                {
                    "schema_version": 1,
                    "job_id": operation.parent_job_id,
                    "operation_id": operation.id,
                    "attempt": attempt.attempt,
                    "fence": attempt.fence,
                    "node_id": operation.node_id,
                    "operation": AgentOperation(operation.kind),
                    "authority_revision": operation.authority_revision,
                    "payload_digest": operation.payload_digest,
                    "payload": operation.payload,
                    "deadline": deadline,
                }
            )

    def _reconcile_agent_upgrade(
        self,
        session: Session,
        node_id: str,
        certificate_serial: str,
        now: datetime,
        capabilities: tuple[str, ...] | None,
        runtime_identity: AgentRuntimeIdentity,
        *,
        operation_id: str | None,
        parent_job_id: str | None,
    ) -> None:
        if (
            operation_id is None
            or capabilities is None
            or AgentOperation.AGENT_UPGRADE.value not in capabilities
        ):
            return
        operation = session.scalar(
            select(StoredOperation)
            .where(
                StoredOperation.id == operation_id,
                StoredOperation.parent_job_id == parent_job_id,
                StoredOperation.node_id == node_id,
                StoredOperation.kind == AgentOperation.AGENT_UPGRADE.value,
                StoredOperation.state.in_(
                    {"queued", "running", "waiting-for-operator"}
                ),
            )
            .order_by(StoredOperation.created_at, StoredOperation.id)
            .with_for_update(of=StoredOperation)
            .execution_options(populate_existing=True)
            .limit(1)
        )
        from vonk_agent_protocol.contracts import AgentUpgradePayload

        from .package_activation import matches_receipt

        receipt = runtime_identity.package_activation
        if operation is None or operation.current_attempt == 0 or receipt is None:
            return
        payload = AgentUpgradePayload.model_validate(operation.payload)
        if not matches_receipt(receipt, payload, node_id):
            return
        if receipt.phase in {"rolled_back", "rollback_failed"}:
            if (
                receipt.phase == "rolled_back"
                and runtime_identity.binary_digest
                != payload.rollback.source.binary_sha256
            ):
                return
            current = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
            )
            # The same terminal receipt is reported until the next install.
            # It must not revoke an operator-authorized retry of that outcome.
            if (
                operation.retry_disposition == "retry"
                and operation.retry_disposition_attempt == operation.current_attempt
                and current is not None
                and isinstance(current.result, dict)
                and current.result.get("package_activation")
                == receipt.model_dump(mode="json")
            ):
                return
            operation.state = "waiting-for-operator"
            operation.retry_disposition = None
            operation.retry_disposition_attempt = None
            operation.updated_at = now
            if current is not None:
                current.state = "failed"
                current.result = {
                    "reason": "agent package " + receipt.phase,
                    "package_activation": receipt.model_dump(mode="json"),
                }
            parent = session.get(Job, operation.parent_job_id)
            if parent is not None:
                parent.state = "waiting-for-operator"
                parent.status_reason = (
                    "Spark package " + receipt.phase + "; rollout stopped"
                )
                parent.updated_at = now
            return
        if receipt.phase != "acknowledged" or (
            runtime_identity.build_digest
            != operation.payload.get("target_build_digest")
            or runtime_identity.binary_digest
            != operation.payload.get("target_binary_digest")
            or runtime_identity.architecture != operation.payload.get("architecture")
            or runtime_identity.self_test_passed is not True
        ):
            return
        evidence = {
            "architecture": runtime_identity.architecture,
            "binary_digest": runtime_identity.binary_digest,
            "build_digest": runtime_identity.build_digest,
            "package_sha256": operation.payload["package_sha256"],
            "package_version": operation.payload["package_version"],
            "self_test_passed": True,
            "status": "upgraded",
            "activation_receipt": receipt.model_dump(mode="json"),
        }
        attempt = session.scalar(
            select(AgentOperationAttempt)
            .where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
            .with_for_update(of=AgentOperationAttempt)
        )
        if attempt is None or attempt.state not in {
            "running",
            "waiting-for-operator",
            "expired",
            "failed",
        }:
            return
        message = AgentResult.model_validate(
            {
                "schema_version": 1,
                "job_id": operation.parent_job_id,
                "operation_id": operation.id,
                "attempt": attempt.attempt,
                "fence": attempt.fence,
                "node_id": operation.node_id,
                "deadline": max(_aware(attempt.lease_deadline), _aware(now)),
                "state": "succeeded",
                "result": evidence,
            }
        )
        # Preserve explicit helper failures as truthful attempt audit. Exact
        # contact reconciles the operation projection, not the historical fact
        # that the signed helper attempt returned failure.
        if attempt.state != "failed":
            attempt.state = "succeeded"
            attempt.result = _document(evidence)
        operation.state = "succeeded"
        operation.retry_disposition = None
        operation.retry_disposition_attempt = None
        operation.updated_at = now
        if self._result_consumer is not None:
            self._result_consumer(session, operation, attempt, message)
        self._aggregate_parent(session, operation.parent_job_id)

    @staticmethod
    def _recipe_build_runtime_matches(
        session: Session,
        operation: StoredOperation,
        runtime_identity: AgentRuntimeIdentity,
    ) -> bool:
        build_id = operation.payload.get("build_id")
        build = (
            session.get(RecipeBuild, build_id) if isinstance(build_id, str) else None
        )
        try:
            report = parse_stored_build_policy(build.policy_report) if build else None
        except RecipeExecutionContractError:
            return False
        return bool(
            build is not None
            and build.builder_node_id == operation.node_id
            and report is not None
            and report.builder_binary_digest == runtime_identity.binary_digest
            and report.artifact_format == BUILD_ARTIFACT_FORMAT
        )

    def _reject_recipe_build_claim(
        self,
        session: Session,
        operation: StoredOperation,
        certificate_serial: str,
        now: datetime,
    ) -> None:
        operation.current_attempt += 1
        operation.state = "failed"
        operation.retry_disposition = None
        operation.retry_disposition_attempt = None
        operation.updated_at = now
        reason = _failure_result(
            "recipe_build_failed",
            "builder runtime identity changed before claim",
            uncertain=False,
        )
        fence = str(uuid.uuid4())
        attempt = AgentOperationAttempt(
            operation_id=operation.id,
            attempt=operation.current_attempt,
            fence=fence,
            lease_deadline=now,
            agent_certificate_serial=certificate_serial,
            state="failed",
            result=reason,
        )
        session.add(attempt)
        session.flush()
        if self._result_consumer is not None:
            self._result_consumer(
                session,
                operation,
                attempt,
                AgentResult.model_validate_json(
                    canonical_message(
                        {
                            "schema_version": 1,
                            "job_id": operation.parent_job_id,
                            "operation_id": operation.id,
                            "attempt": attempt.attempt,
                            "fence": fence,
                            "node_id": operation.node_id,
                            "deadline": _aware(now),
                            "state": "failed",
                            "result": reason,
                        }
                    )
                ),
            )
        self._aggregate_parent(session, operation.parent_job_id)

    def _claim_has_authority(
        self,
        session: Session,
        operation: StoredOperation,
        now: datetime,
        *,
        node: AgentNode,
        protocol_version: int | None,
        capabilities: tuple[str, ...] | None,
        locked_targets: tuple[str, ...],
    ) -> bool:
        job = session.scalar(
            select(Job).where(Job.id == operation.parent_job_id).with_for_update(of=Job)
        )
        if job is None:
            raise ValueError("agent operation lacks its parent job")
        if self._target_scope(job.targets) != locked_targets:
            return False
        current_operation = session.scalar(
            select(StoredOperation)
            .where(StoredOperation.id == operation.id)
            .with_for_update(of=StoredOperation)
        )
        if current_operation is None:
            return False
        if (
            current_operation.kind in _LIFECYCLE_RESTART_OPERATIONS
            and current_operation.current_attempt > 0
            and current_operation.retry_disposition == _RETRY_DISPOSITION
            and current_operation.retry_disposition_attempt
            == current_operation.current_attempt
            and "agent.lifecycle.resume.exact.v1" not in (capabilities or ())
        ):
            current_operation.status_reason = (
                "Spark agent update required before exact recovery; "
                f"retry scheduled at {current_operation.retry_due_at.isoformat()}"
                if current_operation.retry_due_at is not None
                else "Spark agent update required before exact recovery"
            )
            return False
        if (
            current_operation.node_id != node.node_id
            or current_operation.node_id not in job.targets
            or current_operation.authority_revision != job.authority_revision
        ):
            self._record_claim_refusal(
                session,
                operation=current_operation,
                job_id=job.id,
                check="operation-authority-stale",
                kind=current_operation.kind,
            )
            return False
        if (
            (
                current_operation.kind in _WORKLOAD_INTENT_OPERATIONS
                and current_operation.workload_intent_ordinal is None
            )
            or current_operation.workload_intent_ordinal
            != job.payload.get("workload_intent_ordinal")
            or (
                current_operation.workload_intent_ordinal is not None
                and current_operation.workload_intent_ordinal
                != node.workload_intent_ordinal
            )
        ):
            self._record_claim_refusal(
                session,
                operation=current_operation,
                job_id=job.id,
                check="workload-intent-mismatch",
                kind=current_operation.kind,
                operation_intent=current_operation.workload_intent_ordinal,
                parent_intent=job.payload.get("workload_intent_ordinal"),
                node_intent=node.workload_intent_ordinal,
            )
            return False
        if (
            isinstance(job.result, Mapping)
            and job.result.get("cancel_requested") is True
        ):
            self._record_claim_refusal(
                session,
                operation=current_operation,
                job_id=job.id,
                check="parent-cancel-requested",
                kind=current_operation.kind,
            )
            return False
        if (
            node.state != "active"
            or node.revoked_at is not None
            or protocol_version is None
            or node.protocol_version != protocol_version
        ):
            self._record_claim_refusal(
                session,
                operation=current_operation,
                job_id=job.id,
                check="node-not-eligible",
                kind=current_operation.kind,
                node_state=node.state,
                protocol_version=protocol_version,
                node_protocol_version=node.protocol_version,
            )
            return False
        if (
            capabilities is None
            or current_operation.kind not in capabilities
            or not isinstance(node.capabilities, list)
            or current_operation.kind not in node.capabilities
        ):
            self._record_claim_refusal(
                session,
                operation=current_operation,
                job_id=job.id,
                check="capability-unadvertised",
                kind=current_operation.kind,
            )
            return False
        if (
            job.state == "waiting-for-operator"
            and current_operation.state == "waiting-for-operator"
            and current_operation.retry_disposition == _RETRY_DISPOSITION
            and current_operation.retry_disposition_attempt
            == current_operation.current_attempt
        ):
            job.state = "queued"
            job.status_reason = None
            job.updated_at = now
            return True
        if job.state in _TERMINAL_PARENT_STATES:
            self._record_claim_refusal(
                session,
                operation=current_operation,
                job_id=job.id,
                check="parent-not-claimable",
                kind=current_operation.kind,
                parent_state=job.state,
            )
            return False
        return True

    @staticmethod
    def _target_scope(targets: object) -> tuple[str, ...] | None:
        if (
            not isinstance(targets, list)
            or not targets
            or not all(isinstance(node_id, str) for node_id in targets)
            or len(targets) != len(set(targets))
        ):
            return None
        return tuple(sorted(targets))

    @classmethod
    def _lock_operation_scopes(
        cls,
        session: Session,
        operation_ids: tuple[str, ...],
        node_id: str,
    ) -> dict[str, tuple[str, tuple[str, ...]]] | None:
        """Lock hinted target nodes, then parents; callers pin and refresh operations."""
        rows = (
            session.execute(
                select(
                    StoredOperation.id,
                    StoredOperation.parent_job_id,
                    StoredOperation.node_id,
                    Job.targets,
                )
                .join(Job, Job.id == StoredOperation.parent_job_id)
                .where(StoredOperation.id.in_(operation_ids))
            ).all()
            if operation_ids
            else []
        )
        if len(rows) != len(operation_ids):
            return None
        scopes = {}
        for operation_id, parent_id, operation_node, targets in rows:
            scope = cls._target_scope(targets)
            if scope is None or node_id not in scope or operation_node != node_id:
                return None
            scopes[operation_id] = (parent_id, scope)
        if not cls._lock_target_scopes(session, scopes, node_id):
            return None
        return scopes

    @classmethod
    def _lock_target_scopes(
        cls,
        session: Session,
        scopes: dict[str, tuple[str, tuple[str, ...]]],
        node_id: str,
        *,
        nowait: bool = False,
    ) -> bool:
        nodes = sorted(
            {node_id} | {target for _, scope in scopes.values() for target in scope}
        )
        locked_rows: Mapping[str, tuple[Any, ...]] = {}
        if nowait:
            locked_rows = lock_admission_rows(
                session,
                (
                    AdmissionRowLock(
                        "target-agent-nodes",
                        AgentNode,
                        select(AgentNode).where(AgentNode.node_id.in_(nodes)),
                    ),
                    AdmissionRowLock(
                        "target-parent-jobs",
                        Job,
                        select(Job).where(
                            Job.id.in_(
                                sorted({parent_id for parent_id, _ in scopes.values()})
                            )
                        ),
                    ),
                ),
            )
            locked = list(locked_rows.get("target-agent-nodes", ()))
        else:
            locked = list(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(nodes))
                    .order_by(AgentNode.node_id)
                    .with_for_update(of=AgentNode)
                    .execution_options(populate_existing=True)
                )
            )
        if [node.node_id for node in locked] != nodes:
            return False
        parent_ids = sorted({parent_id for parent_id, _ in scopes.values()})
        parents = (
            {
                job.id: job
                for job in (
                    locked_rows.get("target-parent-jobs", ())
                    if nowait
                    else session.scalars(
                        select(Job)
                        .where(Job.id.in_(parent_ids))
                        .order_by(Job.id)
                        .with_for_update(of=Job)
                        .execution_options(populate_existing=True)
                    )
                )
            }
            if parent_ids
            else {}
        )
        return not any(
            parent_id not in parents
            or cls._target_scope(parents[parent_id].targets) != scope
            for parent_id, scope in scopes.values()
        )

    def heartbeat(
        self,
        fence: AgentFence,
        progress: Mapping[str, object] | None,
        lease_seconds: int,
        *,
        source: AgentSource | None = None,
    ) -> AgentDirective:
        self._mark_started()
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        with self._sessions.begin() as session:
            operation, attempt = self._active(
                session,
                fence,
                source=source,
                allow_superseded_cancellation=True,
                # A heartbeat and the exact attempt's own outcome may both
                # re-acquire a lapsed lease inside the operation's launch
                # budget; a different fence or a spent budget may not.
                allow_lapsed_renewal=True,
            )
            now = self._clock()
            parent = session.get(Job, operation.parent_job_id)
            node = session.get(AgentNode, operation.node_id)
            superseded = bool(
                node is not None
                and operation.workload_intent_ordinal is not None
                and operation.workload_intent_ordinal != node.workload_intent_ordinal
            )
            if superseded:
                cancellation_deadline = superseded_cancellation_deadline(
                    None if parent is None else parent.result
                )
                if cancellation_deadline is None:
                    raise StaleAgentAttempt(
                        "superseded cancellation authority is invalid"
                    )
                if _aware(now) >= cancellation_deadline:
                    raise StaleAgentAttempt("superseded cancellation authority expired")
                deadline = min(
                    cancellation_deadline,
                    max(
                        _aware(attempt.lease_deadline),
                        _aware(now) + timedelta(seconds=lease_seconds),
                    ),
                )
                attempt.lease_deadline = deadline
                return AgentDirective(
                    schema_version=1,
                    job_id=operation.parent_job_id,
                    operation_id=operation.id,
                    attempt=attempt.attempt,
                    fence=attempt.fence,
                    node_id=operation.node_id,
                    deadline=deadline,
                    cancel_requested=True,
                )
            deadline = max(
                _aware(attempt.lease_deadline),
                _aware(now) + timedelta(seconds=lease_seconds),
            )
            message = AgentProgress.model_validate(
                {
                    "schema_version": 1,
                    "job_id": operation.parent_job_id,
                    "operation_id": operation.id,
                    "attempt": attempt.attempt,
                    "fence": attempt.fence,
                    "node_id": operation.node_id,
                    "deadline": deadline,
                    "progress": progress,
                }
            )
            write_progress = message.progress is None
            if message.progress is not None:
                try:
                    current_progress = dict(message.progress)
                    if (
                        operation.kind == AgentOperation.ARTIFACT_DISTRIBUTION.value
                        and attempt.progress
                    ):
                        # A restarted transfer walks already durable objects again.
                        # Replayed offsets are not loss of retained operation bytes.
                        for key in ("completed_bytes", "completed_items"):
                            if key in current_progress and key in attempt.progress:
                                current_progress[key] = max(
                                    current_progress[key], attempt.progress[key]
                                )
                    validated = validate_progress_update(
                        attempt.progress, current_progress, partial=False
                    )
                    write_progress = progress_write_due(
                        attempt.progress, validated, _aware(now)
                    )
                    if write_progress:
                        attempt.progress = observe_progress(
                            attempt.progress, validated, _aware(now)
                        )
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"operation progress is invalid: {error}"
                    ) from error
            if write_progress:
                attempt.lease_deadline = deadline
                operation.updated_at = now
            else:
                deadline = _aware(attempt.lease_deadline)
            parent = session.get(Job, operation.parent_job_id)
            cancel_requested = bool(
                parent is not None
                and isinstance(parent.result, Mapping)
                and parent.result.get("cancel_requested") is True
            )
            if (
                operation.kind == AgentOperation.ARTIFACT_DISTRIBUTION.value
                and not cancel_requested
            ):
                # Large model copies outlive their initial one-hour grant.
                # Only the authenticated, currently fenced operation may
                # renew its exact node/plan, before that grant expires. Keep
                # renewals sparse and never resurrect revoked/expired access.
                session.execute(
                    update(ArtifactDistributionAssignment)
                    .where(
                        ArtifactDistributionAssignment.node_id == operation.node_id,
                        ArtifactDistributionAssignment.plan_digest
                        == operation.authority_revision,
                        ArtifactDistributionAssignment.state == "active",
                        ArtifactDistributionAssignment.expires_at > now,
                        ArtifactDistributionAssignment.expires_at
                        < now + timedelta(minutes=30),
                    )
                    .values(expires_at=now + timedelta(hours=1), updated_at=now)
                )
            return AgentDirective(
                schema_version=message.schema_version,
                job_id=message.job_id,
                operation_id=message.operation_id,
                attempt=message.attempt,
                fence=message.fence,
                node_id=message.node_id,
                deadline=deadline,
                cancel_requested=cancel_requested,
            )

    def known_superseded_cancellation(
        self, fence: AgentProgress, *, source: AgentSource | None = None
    ) -> bool:
        """Identify an exact old cancellation for a benign heartbeat response."""
        with self._sessions() as session:
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.fence == fence.fence
                )
            )
            operation = (
                session.get(StoredOperation, attempt.operation_id)
                if attempt is not None
                else None
            )
            parent = (
                session.get(Job, operation.parent_job_id)
                if operation is not None
                else None
            )
            if (
                attempt is None
                or operation is None
                or parent is None
                or operation.kind not in _WORKLOAD_INTENT_OPERATIONS
                or operation.id != fence.operation_id
                or operation.parent_job_id != fence.job_id
                or operation.node_id != fence.node_id
                or attempt.attempt != fence.attempt
                or operation.current_attempt != attempt.attempt
                or _aware(fence.deadline) > _aware(attempt.lease_deadline)
                or operation.workload_intent_ordinal
                != parent.payload.get("workload_intent_ordinal")
                or not isinstance(parent.result, Mapping)
                or parent.result.get("cancel_requested") is not True
                or self._target_scope(parent.targets) is None
                or operation.node_id not in parent.targets
            ):
                return False
            contact_serial = (
                source.identity.certificate_serial
                if source is not None
                else attempt.agent_certificate_serial
            )
            identity = self._lock_identity(session, operation.node_id, contact_serial)
            now = self._clock()
            if identity is None or not self._identity_is_active(*identity, now):
                return False
            node, _certificate = identity
            return bool(
                node.state == "active"
                and node.revoked_at is None
                and (
                    operation.state == "cancelled"
                    or (
                        operation.workload_intent_ordinal is not None
                        and operation.workload_intent_ordinal
                        < node.workload_intent_ordinal
                    )
                )
            )

    def succeed(self, fence: AgentFence, result: Mapping[str, object]) -> None:
        self._finish(fence, "succeeded", result=result, reason=None)

    def fail(self, fence: AgentFence, reason: str) -> None:
        self._finish(
            fence,
            "failed",
            result=_failure_result("operation_failed", reason, uncertain=False),
            reason=None,
        )

    def wait_for_operator(self, fence: AgentFence, reason: str) -> None:
        self._finish(
            fence,
            "waiting-for-operator",
            result=_failure_result(
                "operation_requires_operator", reason, uncertain=True
            ),
            reason=None,
        )

    def uncertain(self, fence: AgentFence, reason: str) -> None:
        """Persist an ambiguous mutation outcome and require inspection first."""
        self._finish(
            fence,
            "waiting-for-operator",
            result=_failure_result(
                "operation_outcome_uncertain", reason, uncertain=True
            ),
            reason=None,
        )

    def record_result(
        self, message: AgentResult, *, source: AgentSource | None = None
    ) -> None:
        """Persist one exact agent result and consume it in the same transaction."""
        self._finish(
            message,
            message.state,
            result=message.result,
            reason=None,
            source=source,
        )

    @staticmethod
    def _schedule_safe_retry(
        operation: StoredOperation,
        now: datetime,
        retry_after_seconds: int | None = None,
    ) -> None:
        if operation.kind not in _RESTART_REISSUE_OPERATIONS:
            raise ValueError(
                "operation cannot be reissued without effect reconciliation"
            )
        retry_after = (
            None
            if retry_after_seconds is None
            else _aware(now) + timedelta(seconds=retry_after_seconds)
        )
        due = RecoveryPolicy().next_attempt(
            operation.id,
            operation.current_attempt,
            _aware(now),
            retry_after=retry_after,
            ongoing_intent=True,
        )
        # Exact-resume support reconciles the prior effect before any new work.
        # Bound frequency, not the lifetime of current authorized intent: a long
        # outage must not require an operator to retire and recreate this job.
        assert due is not None
        operation.retry_disposition = _RETRY_DISPOSITION
        operation.retry_disposition_attempt = operation.current_attempt
        operation.retry_due_at = due
        previous_reason = operation.status_reason
        schedule_reason = (
            f"exact {operation.kind} interrupted; retry scheduled at {due.isoformat()}"
        )
        operation.status_reason = (
            f"{previous_reason}; {schedule_reason}"
            if isinstance(previous_reason, str)
            and previous_reason.startswith("attempt ")
            and "lease expired" in previous_reason
            else schedule_reason
        )

    def record_late_result(
        self, message: AgentResult, *, source: AgentSource | None = None
    ) -> bool:
        """Retain stale effects; close only a proved cancellation of the old order.

        Only the authenticated node may submit the exact historical fence.
        Certificate rotation may change its current TLS credential while the
        durable receipt still names the original certificate. Old success and
        failure stay diagnostic. A typed cancellation acknowledgement may
        retire only the exact superseded order after the agent confirms its
        host action has ceased; it never applies that order's desired effect.
        """
        self._mark_started()
        with self._sessions.begin() as session:
            hint = session.execute(
                select(
                    StoredOperation.id,
                    StoredOperation.node_id,
                    AgentOperationAttempt.agent_certificate_serial,
                    StoredOperation.parent_job_id,
                )
                .join(
                    AgentOperationAttempt,
                    AgentOperationAttempt.operation_id == StoredOperation.id,
                )
                .where(AgentOperationAttempt.fence == message.fence)
            ).one_or_none()
            if hint is None:
                raise StaleAgentAttempt(
                    "agent operation lease, certificate, or fence is stale"
                )
            operation_id, node_id, serial, parent_job_id = hint
            scopes = self._lock_operation_scopes(session, (operation_id,), node_id)
            if scopes is None or scopes[operation_id][0] != parent_job_id:
                raise StaleAgentAttempt("agent operation authority is stale")
            # Certificate rotation replaces the TLS identity while retaining
            # the node's durable ledger. Authenticate current contact under
            # its fresh certificate; the expired attempt remains bound to the
            # original serial and fence for diagnostics only.
            contact_serial = (
                source.identity.certificate_serial if source is not None else serial
            )
            identity = self._lock_identity(session, node_id, contact_serial)
            now = self._clock()
            if identity is None or not self._identity_is_active(*identity, now):
                raise StaleAgentAttempt("agent certificate is no longer active")
            node, certificate = identity
            self._consume_contact(session, source, node, certificate)
            parent = session.get(Job, parent_job_id, with_for_update=True)
            operation = session.get(StoredOperation, operation_id, with_for_update=True)
            attempt = session.scalar(
                select(AgentOperationAttempt)
                .where(AgentOperationAttempt.fence == message.fence)
                .with_for_update(of=AgentOperationAttempt)
            )
            if (
                parent is None
                or operation is None
                or attempt is None
                or node.state != "active"
                or node.revoked_at is not None
                or self._target_scope(parent.targets) != scopes[operation_id][1]
                or operation.authority_revision != parent.authority_revision
                or operation.parent_job_id != message.job_id
                or operation.id != message.operation_id
                or operation.node_id != message.node_id
                or attempt.operation_id != operation.id
                or attempt.attempt != message.attempt
                or attempt.agent_certificate_serial != serial
                or _aware(message.deadline) > _aware(attempt.lease_deadline)
            ):
                raise StaleAgentAttempt(
                    "agent operation authority or expired attempt is stale"
                )
            validate_result_for_operation(
                operation.kind, message.result, state=message.state
            )
            evidence = _document(message.result)
            if message.state in {"failed", "waiting-for-operator"}:
                evidence = sanitize_failure_evidence(evidence)
            if _aware(message.deadline) < _aware(attempt.lease_deadline):
                # The agent may have lost a heartbeat renewal response before
                # learning that this order was cancelled. Its original fence
                # identifies the old order, but the old deadline cannot prove
                # quiescence or replace a result under the renewed authority.
                if (
                    message.state == "cancelled"
                    and operation.kind in _WORKLOAD_INTENT_OPERATIONS
                    and operation.current_attempt == attempt.attempt
                    and operation.workload_intent_ordinal
                    == parent.payload.get("workload_intent_ordinal")
                    and isinstance(parent.result, Mapping)
                    and superseded_cancellation_deadline(parent.result) is not None
                    and (
                        operation.state == "cancelled"
                        or (
                            operation.workload_intent_ordinal is not None
                            and operation.workload_intent_ordinal
                            < node.workload_intent_ordinal
                        )
                    )
                ):
                    return False
                raise StaleAgentAttempt("agent operation renewal deadline is stale")
            if attempt.state == message.state and attempt.result == evidence:
                return False
            superseded_intent = (
                operation.workload_intent_ordinal is not None
                and operation.workload_intent_ordinal != node.workload_intent_ordinal
            )
            if (
                message.state == "cancelled"
                and superseded_intent
                and operation.kind in _WORKLOAD_INTENT_OPERATIONS
                and operation.workload_intent_ordinal
                == parent.payload.get("workload_intent_ordinal")
                and isinstance(parent.result, Mapping)
                and superseded_cancellation_deadline(parent.result) is not None
                and operation.current_attempt == attempt.attempt
                and operation.state in {"running", "waiting-for-operator"}
                and attempt.state in {"running", "expired"}
                and attempt.result is None
                and (
                    (
                        operation.kind == AgentOperation.RECIPE_JOB_RUN.value
                        and isinstance(message.result, RecipeJobRunResult)
                        and message.result.exit_code == 130
                        and not message.result.outputs
                    )
                    or (
                        operation.kind != AgentOperation.RECIPE_JOB_RUN.value
                        and evidence.get("error_code") == "operation_cancelled"
                        and evidence.get("uncertain") is not True
                    )
                )
            ):
                attempt.state = "cancelled"
                attempt.result = evidence
                operation.state = "cancelled"
                operation.retry_disposition = None
                operation.retry_disposition_attempt = None
                operation.retry_due_at = None
                operation.updated_at = now
                if self._result_consumer is not None:
                    self._result_consumer(session, operation, attempt, message)
                self._aggregate_parent(session, operation.parent_job_id)
                return True
            if attempt.state == "running" and (
                _aware(attempt.lease_deadline) <= _aware(now) or superseded_intent
            ):
                attempt.state = "expired"
                if (
                    operation.current_attempt == attempt.attempt
                    and operation.state == "running"
                ):
                    operation.state = "waiting-for-operator"
                    operation.status_reason = _lease_expiry_reason(
                        operation, attempt, node, now
                    )
                    operation.updated_at = now
                    if parent.state in {"queued", "running"}:
                        self._aggregate_parent(session, operation.parent_job_id)
            if attempt.state != "expired":
                raise StaleAgentAttempt("agent operation attempt is not expired")
            if attempt.result is not None:
                if attempt.result != evidence:
                    raise StaleAgentAttempt("expired attempt evidence changed")
                return True
            attempt.result = evidence
            return True

    def _finish(
        self,
        fence: AgentFence,
        state: AgentResultState,
        *,
        result: Mapping[str, object] | None,
        reason: str | None,
        source: AgentSource | None = None,
    ) -> None:
        self._mark_started()
        with self._sessions.begin() as session:
            operation, attempt = self._active(
                session,
                fence,
                source=source,
                allow_superseded_cancellation=state == "cancelled",
                # An outcome is the same launch decision the heartbeat renewal
                # already allows: an attempt that is still the operation's own
                # current attempt inside its declared readiness budget may report
                # what it observed.  A newer attempt, a stopped attempt, and a
                # spent budget all still refuse it, so this never re-blesses an
                # abandoned effect.
                allow_lapsed_renewal=True,
            )
            if isinstance(fence, AgentResult) and _aware(fence.deadline) != _aware(
                attempt.lease_deadline
            ):
                raise StaleAgentAttempt("agent operation renewal deadline is stale")
            now = self._clock()
            node = session.get(AgentNode, operation.node_id)
            parent = session.get(Job, operation.parent_job_id)
            if parent is None:
                raise StaleAgentAttempt("agent operation lacks its parent job")
            superseded = bool(
                node is not None
                and operation.workload_intent_ordinal is not None
                and operation.workload_intent_ordinal != node.workload_intent_ordinal
            )
            if superseded:
                cancellation_deadline = superseded_cancellation_deadline(
                    None if parent is None else parent.result
                )
                if (
                    state != "cancelled"
                    or cancellation_deadline is None
                    or _aware(now) >= cancellation_deadline
                ):
                    raise StaleAgentAttempt(
                        "superseded operation has no completion authority"
                    )
            if isinstance(fence, AgentResult):
                if fence.state != state or (
                    result is not None and _document(fence.result) != _document(result)
                ):
                    raise ValueError("agent result does not match requested completion")
                message = fence
            else:
                canonical_result = (
                    result if result is not None else {"reason": self._reason(reason)}
                )
                message = AgentResult.model_validate_json(
                    canonical_message(
                        {
                            "schema_version": 1,
                            "job_id": operation.parent_job_id,
                            "operation_id": operation.id,
                            "attempt": attempt.attempt,
                            "fence": attempt.fence,
                            "node_id": operation.node_id,
                            "deadline": _aware(attempt.lease_deadline),
                            "state": state,
                            "result": canonical_result,
                        }
                    )
                )
            validate_result_for_operation(
                operation.kind,
                message.result,
                state=message.state,
            )
            if state in {"failed", "waiting-for-operator"}:
                try:
                    raw_result = _document(message.result)
                    diagnostics = raw_result.pop("diagnostics", None)
                    if (
                        operation.kind == AgentOperation.RECIPE_JOB_RUN.value
                        and "exit_code" in raw_result
                    ):
                        # Preserve the typed output manifest and process receipt.
                        # Generic log truncation must not rewrite their structure.
                        message_result = raw_result
                        raw_reason = message_result.get("reason")
                        if isinstance(raw_reason, str):
                            message_result["reason"] = safe_text(raw_reason)
                    else:
                        message_result = sanitize_failure_evidence(raw_result)
                    if diagnostics is not None:
                        message_result["diagnostics"] = sanitize_diagnostics(
                            diagnostics
                        ).model_dump(mode="json")
                    message = AgentResult.model_validate_json(
                        canonical_message(
                            {
                                **message.model_dump(mode="json"),
                                "result": message_result,
                            }
                        )
                    )
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"operation failure evidence is invalid: {error}"
                    ) from error
            else:
                message_result = _document(message.result)
            if (
                state == "succeeded"
                and operation.kind == AgentOperation.ARTIFACT_DISTRIBUTION.value
            ):
                # Final authoritative evidence closes a last sample that may
                # have been coalesced immediately before result publication.
                final_progress = {
                    "phase": "completed",
                    "completed_bytes": message_result["downloaded_bytes"],
                }
                if attempt.progress and attempt.progress.get("total_items") is not None:
                    final_progress["completed_items"] = attempt.progress["total_items"]
                final_progress = validate_progress_update(
                    attempt.progress, final_progress
                )
                attempt.progress = observe_progress(
                    attempt.progress, final_progress, _aware(now)
                )
            attempt.result = message_result
            attempt.state = state
            safe_retry = _safe_retry_failure(
                operation.kind, state, message_result
            ) and not (
                isinstance(parent.result, Mapping)
                and parent.result.get("cancel_requested") is True
            )
            operation.state = "waiting-for-operator" if safe_retry else state
            operation.updated_at = now
            if safe_retry:
                retry_after_seconds = message_result.get("retry_after_seconds")
                self._schedule_safe_retry(
                    operation,
                    now,
                    retry_after_seconds if type(retry_after_seconds) is int else None,
                )
            if self._result_consumer is not None:
                self._result_consumer(session, operation, attempt, message)
            self._aggregate_parent(session, operation.parent_job_id)
        # A result consumer can atomically make the next durable recipe phase
        # queueable; wake long-polling agents only after that transaction commits.
        self.notify_available()

    def _active(
        self,
        session: Session,
        fence: AgentFence,
        *,
        source: AgentSource | None = None,
        allow_superseded_cancellation: bool = False,
        allow_lapsed_renewal: bool = False,
    ) -> tuple[StoredOperation, AgentOperationAttempt]:
        token = self._fence_token(fence)
        identity_hint = session.execute(
            select(
                StoredOperation.id,
                StoredOperation.node_id,
                AgentOperationAttempt.agent_certificate_serial,
                StoredOperation.parent_job_id,
            )
            .join(
                AgentOperationAttempt,
                AgentOperationAttempt.operation_id == StoredOperation.id,
            )
            .where(AgentOperationAttempt.fence == token)
        ).one_or_none()
        if identity_hint is None:
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        operation_id, node_id, certificate_serial, parent_job_id = identity_hint
        scopes = self._lock_operation_scopes(session, (operation_id,), node_id)
        if scopes is None or scopes[operation_id][0] != parent_job_id:
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        identity = self._lock_identity(session, node_id, certificate_serial)
        now = self._clock()
        if identity is None or not self._identity_is_active(*identity, now):
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        node, certificate = identity
        self._consume_contact(session, source, node, certificate)
        parent = session.scalar(
            select(Job).where(Job.id == parent_job_id).with_for_update(of=Job)
        )
        if (
            parent is None
            or parent.state not in {"queued", "running"}
            or node.node_id not in parent.targets
        ):
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        operation = session.scalar(
            select(StoredOperation)
            .where(StoredOperation.id == operation_id)
            .with_for_update(of=StoredOperation)
            .execution_options(populate_existing=True)
        )
        if operation is None:
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        if (
            self._target_scope(parent.targets) != scopes[operation_id][1]
            or operation.parent_job_id != parent_job_id
            or operation.node_id != node.node_id
            or operation.authority_revision != parent.authority_revision
            or (
                operation.kind in _WORKLOAD_INTENT_OPERATIONS
                and operation.workload_intent_ordinal is None
            )
            or operation.workload_intent_ordinal
            != parent.payload.get("workload_intent_ordinal")
            or (
                operation.workload_intent_ordinal is not None
                and operation.workload_intent_ordinal != node.workload_intent_ordinal
                and not (
                    allow_superseded_cancellation
                    and operation.kind in _WORKLOAD_INTENT_OPERATIONS
                    and operation.workload_intent_ordinal < node.workload_intent_ordinal
                    and isinstance(parent.result, Mapping)
                    and parent.result.get("cancel_requested") is True
                )
            )
            or node.state != "active"
            or node.revoked_at is not None
            or not isinstance(node.capabilities, list)
            or operation.kind not in node.capabilities
        ):
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        attempt = session.scalar(
            select(AgentOperationAttempt)
            .where(
                AgentOperationAttempt.fence == token,
                AgentOperationAttempt.operation_id == operation.id,
            )
            .with_for_update(of=AgentOperationAttempt)
            .execution_options(populate_existing=True)
        )
        if (
            attempt is None
            or operation.state != "running"
            or (not isinstance(fence, str) and operation.parent_job_id != fence.job_id)
            or (not isinstance(fence, str) and operation.id != fence.operation_id)
            or (not isinstance(fence, str) and operation.node_id != fence.node_id)
            or (
                not isinstance(fence, str)
                and operation.current_attempt != fence.attempt
            )
            or attempt.operation_id != operation.id
            or operation.current_attempt != attempt.attempt
            or attempt.state != "running"
            or (
                _aware(attempt.lease_deadline) <= _aware(now)
                and not (
                    allow_lapsed_renewal
                    # The exact fence that holds the attempt may re-acquire it
                    # while the operation's own immutable start budget is still
                    # open.  The lease still decides when another owner may take
                    # over, so this restores a healthy executor without opening
                    # the fence to anyone else.
                    and _lapsed_renewal_allowed(operation, now)
                )
            )
        ):
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )
        self._record_contact(
            session,
            node,
            certificate,
            now,
            None,
            None,
            None,
            None,
        )
        return operation, attempt

    @staticmethod
    def _capabilities(
        capabilities: Sequence[str] | None,
    ) -> tuple[str, ...] | None:
        """Normalize the negotiated capability intersection.

        Agents may be newer than the Controller and advertise capabilities this
        Controller does not know yet.  Those capabilities are intentionally
        ignored for this session; operation dispatch already checks the
        normalized set, so the effective contract is the intersection of both
        sides.  Required capabilities are still enforced by
        ``_validate_agent_contract`` below.
        """
        if capabilities is None:
            return None
        if isinstance(capabilities, (str, bytes)):
            raise TypeError("agent capabilities are invalid")
        values = tuple(capabilities)
        if not values or any(
            not isinstance(value, str) or not value for value in values
        ):
            raise ValueError("agent capabilities are invalid")
        return tuple(
            sorted(
                {
                    value
                    for value in values
                    if value in _KNOWN_CAPABILITIES
                    or re.fullmatch(
                        r"runtime\.preflight\.fingerprint\.[0-9a-f]{64}", value
                    )
                }
            )
        )

    @staticmethod
    def _validate_agent_contract(
        protocol_version: int | None,
        capabilities: tuple[str, ...] | None,
        runtime_identity: AgentRuntimeIdentity,
    ) -> None:
        if (
            protocol_version is None
            or protocol_version != 3
            or capabilities is None
            or "agent.runtime.rust.v1" not in capabilities
        ):
            raise ValueError("Rust agent capability negotiation is incomplete")
        receipt_key = runtime_identity.observation_receipt_public_key
        receipt_capable = "recipe.run.inspect.receipt.v1" in capabilities
        if receipt_capable and not (
            isinstance(receipt_key, str) and len(receipt_key) == 64
        ):
            raise ValueError("agent observation receipt identity is incomplete")

    @staticmethod
    def _record_contact(
        session: Session,
        node: AgentNode,
        certificate: AgentCertificate,
        now: datetime,
        protocol_version: int | None,
        capabilities: tuple[str, ...] | None,
        runtime_identity: AgentRuntimeIdentity | None,
        hostname: str | None,
    ) -> None:
        current = None if node.last_seen_at is None else _aware(node.last_seen_at)
        observed = _aware(now)
        if current is None or observed > current:
            node.last_seen_at = observed
        contact_time = node.last_seen_at
        if contact_time is None:
            raise ValueError("agent contact timestamp is unavailable")
        if protocol_version is not None:
            node.protocol_version = protocol_version
        if capabilities is not None:
            node.capabilities = list(capabilities)
        if hostname is not None:
            profile = session.scalar(
                select(AgentNodeProfile)
                .where(AgentNodeProfile.node_id == node.node_id)
                .with_for_update(of=AgentNodeProfile)
            )
            if profile is not None and profile.hostname != hostname:
                profile.hostname = hostname
        if runtime_identity is not None:
            receipt_key = runtime_identity.observation_receipt_public_key
            if (
                receipt_key is not None
                and node.observation_receipt_public_key is not None
                and node.observation_receipt_public_key != receipt_key
            ):
                raise ValueError("agent observation receipt key changed")
            if (
                isinstance(receipt_key, str)
                and node.observation_receipt_public_key is None
            ):
                # The first authenticated contact binds the immutable receipt
                # identity; subsequent contacts remain change-protected above.
                node.observation_receipt_public_key = receipt_key
            node.architecture = runtime_identity.architecture
            node.semantic_version = runtime_identity.semantic_version
            node.build_digest = runtime_identity.build_digest
            node.binary_digest = runtime_identity.binary_digest
            node.self_test_passed = runtime_identity.self_test_passed
            node.contact_certificate_serial = certificate.serial
            node.contact_observation_digest = hashlib.sha256(
                canonical_message(
                    {
                        "certificate_fingerprint": certificate.fingerprint,
                        "certificate_serial": certificate.serial,
                        "node_id": node.node_id,
                        "observed_at": _aware(contact_time).isoformat(),
                        "hostname": hostname,
                        "runtime_identity": runtime_identity,
                    }
                )
            ).hexdigest()

    @staticmethod
    def _runtime_identity(
        value: AgentRuntimeIdentity | Mapping[str, object] | None,
    ) -> AgentRuntimeIdentity:
        if value is None:
            raise ValueError("agent runtime identity is required")
        try:
            return AgentRuntimeIdentity.model_validate(value)
        except (TypeError, ValidationError) as error:
            raise ValueError("agent runtime identity is invalid") from error

    def _consume_contact(
        self,
        session: Session,
        source: AgentSource | None,
        node: AgentNode,
        certificate: AgentCertificate,
    ) -> None:
        if source is None:
            return
        identity = source.identity
        if (
            identity.node_id != node.node_id
            or identity.certificate_serial != certificate.serial
            or identity.certificate_fingerprint != certificate.fingerprint
            or identity.verified is not True
        ):
            raise ValueError("agent contact source does not match its locked identity")
        if self._contact_consumer is None:
            raise RuntimeError("agent contact consumer is not configured")
        self._contact_consumer(session, source)

    @staticmethod
    def _project_artifact_job_expiry(
        session: Session,
        operation: StoredOperation,
        now: datetime,
    ) -> bool:
        if operation.kind != AgentOperation.RECIPE_JOB_RUN.value:
            return False
        artifact_job = session.scalar(
            select(ArtifactJob)
            .where(ArtifactJob.operation_id == operation.parent_job_id)
            .with_for_update(of=ArtifactJob)
        )
        if artifact_job is None:
            return False
        reason = (
            "artifact job agent lease expired; the uncertain attempt was fenced "
            "and late results will be rejected"
        )
        operation.state = "failed"
        operation.updated_at = now
        if artifact_job.state not in {"succeeded", "failed", "cancelled"}:
            artifact_job.state = "failed"
            artifact_job.status_reason = reason
            artifact_job.result_evidence = {
                "failure_kind": "agent-lease-expired",
                "recoverable": True,
                "late_results_accepted": False,
            }
            artifact_job.completed_at = now
            artifact_job.updated_at = now
        return True

    @staticmethod
    def _fence_token(fence: AgentFence) -> str:
        if isinstance(fence, str):
            return fence
        if isinstance(fence, (AgentClaim, AgentProgress, AgentResult)):
            return fence.fence
        raise StaleAgentAttempt("agent operation lease, certificate, or fence is stale")

    @staticmethod
    def _reason(reason: str | None) -> str:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("failure reason is required")
        return redact_text(reason)[:1024]

    @staticmethod
    def _lock_identity(
        session: Session,
        node_id: str,
        certificate_serial: str,
    ) -> tuple[AgentNode, AgentCertificate] | None:
        node = session.scalar(
            select(AgentNode)
            .where(AgentNode.node_id == node_id)
            .with_for_update(of=AgentNode)
        )
        if node is None:
            return None
        certificate = session.scalar(
            select(AgentCertificate)
            .where(
                AgentCertificate.serial == certificate_serial,
                AgentCertificate.node_id == node_id,
            )
            .with_for_update(of=AgentCertificate)
        )
        return None if certificate is None else (node, certificate)

    @staticmethod
    def _identity_is_active(
        node: AgentNode,
        certificate: AgentCertificate,
        now: datetime,
    ) -> bool:
        return (
            node.state == "active"
            and node.revoked_at is None
            and certificate.state == "active"
            and certificate.revoked_at is None
            and _aware(certificate.not_before) <= _aware(now)
            and _aware(certificate.not_after) > _aware(now)
        )

    def _aggregate_parent(self, session: Session, parent_job_id: str) -> None:
        job = session.scalar(
            select(Job).where(Job.id == parent_job_id).with_for_update(of=Job)
        )
        if job is None:
            raise KeyError(parent_job_id)
        if (
            job.kind == "recipe.build.v1"
            and isinstance(job.result, Mapping)
            and job.result.get("cancel_requested") is True
            and job.result.get("cancelled") is not True
        ):
            # A stopped build needs a separate cleanup receipt before its
            # reservation can be released, even if completion raced removal.
            job.state = "waiting-for-operator"
            job.updated_at = self._clock()
            return
        operations = list(
            session.scalars(
                select(StoredOperation)
                .where(StoredOperation.parent_job_id == parent_job_id)
                .order_by(StoredOperation.created_at, StoredOperation.id)
            )
        )
        retrying = [
            operation
            for operation in operations
            if operation.state == "waiting-for-operator"
            and operation.retry_disposition == _RETRY_DISPOSITION
            and operation.retry_disposition_attempt == operation.current_attempt
            and operation.retry_due_at is not None
        ]
        if (
            retrying
            and all(
                operation.state == "succeeded" or operation in retrying
                for operation in operations
            )
            and not (
                isinstance(job.result, Mapping)
                and job.result.get("cancel_requested") is True
            )
        ):
            job.state = "queued"
            job.status_reason = retrying[0].status_reason
            job.updated_at = self._clock()
            return
        if (
            job.kind == "agent-upgrade"
            and job.state == "waiting-for-operator"
            and set(job.targets) - {operation.node_id for operation in operations}
        ):
            # Sequential agent upgrades intentionally materialize one target at
            # a time. If the next target drifted ineligible, preserve the
            # service's specific operator-facing reason instead of declaring the
            # job successful merely because every materialized operation passed.
            return
        terminal = _AGGREGATE_FINAL_STATES
        if not operations or any(
            operation.state not in terminal for operation in operations
        ):
            return
        states = {operation.state for operation in operations}
        if "failed" in states:
            state = "failed"
        elif "waiting-for-operator" in states:
            state = "waiting-for-operator"
        elif "cancelled" in states:
            state = "cancelled"
        else:
            state = "succeeded"
        job.state = state
        job.updated_at = self._clock()
        if state == "succeeded":
            job.status_reason = None
            return
        for operation in operations:
            if operation.state != state:
                continue
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
            )
            if attempt is not None and attempt.result is not None:
                reason = attempt.result.get("reason")
                if not isinstance(reason, str):
                    reason = attempt.result.get("error_code")
                if isinstance(reason, str):
                    if job.kind == "agent-upgrade":
                        package = job.payload.get("package")
                        node = session.get(AgentNode, operation.node_id)
                        if isinstance(package, Mapping):
                            reason = operator_agent_upgrade_reason(
                                node_id=operation.node_id,
                                attempt_count=operation.current_attempt,
                                package=package,
                                observed_semantic_version=(
                                    None if node is None else node.semantic_version
                                ),
                                observed_binary_digest=(
                                    None if node is None else node.binary_digest
                                ),
                                observed_build_digest=(
                                    None if node is None else node.build_digest
                                ),
                                raw_reason=reason,
                                retry_queued=(
                                    operation.retry_disposition == _RETRY_DISPOSITION
                                    and operation.retry_disposition_attempt
                                    == operation.current_attempt
                                ),
                            )
                    job.status_reason = redact_text(reason)[:1024]
                    return
        job.status_reason = None
