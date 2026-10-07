"""Claim notes for the node-scoped agent queue."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentClaim, InvalidRequestReason

from .. import agent_operation_states
from ..agent_job_contract import ClaimFacts
from ..auth import AgentSource
from ..categorized_errors import InvalidValue
from ..logging import redact_text
from ..models import AgentCertificate, AgentNode, AgentOperationAttempt, Job
from ..models import AgentOperation as StoredOperation
from .contracts import (
    _CLAIM_REFUSAL_PREFIX,
    _CONCLUDED_OUTCOMES,
    _DATABASE_REPOLL_SECONDS,
    _LOGGER,
)
from .evidence import _is_refusal_reason
from .predicates import (
    _claim_condition_facts,
    _claim_note_reason,
    _claim_predicate,
    _claim_refusal_reason,
    _held_claim_conditions,
    _refusal_reason,
)
from .stored import column_field, column_is_document, column_value

if TYPE_CHECKING:
    from .service import AgentJobService


def claim_next_operation(
    self: AgentJobService,
    node_id: str,
    certificate_serial: str,
    wait_seconds: float = 0,
    *,
    runtime_identity: object,
    preflight_fingerprint: str | None = None,
    hostname: str | None = None,
    source: AgentSource | None = None,
) -> AgentClaim | None:
    self._mark_started()
    if (
        not node_id.strip()
        or not certificate_serial.strip()
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
            preflight_fingerprint is not None
            and re.fullmatch(r"[0-9a-f]{64}", preflight_fingerprint) is None
        )
    ):
        raise InvalidValue(
            "node and certificate are required",
            reason=InvalidRequestReason.INCOMPLETE,
        )
    running = self._runtime_identity(runtime_identity)
    if self._advance_node is not None:
        try:
            self._advance_node(node_id)
        except Exception as error:  # noqa: BLE001 - a poll must still claim work
            # Rollout progression is retried on the next poll; it must not
            # turn into a claim failure for unrelated work on this Spark.
            _LOGGER.warning(
                "agent upgrade rollout advance failed for %s: %s",
                node_id,
                redact_text(error),
            )
    deadline = self._monotonic() + wait_seconds
    with self._available:
        while True:
            claim = self._claim_once(
                node_id,
                certificate_serial,
                running,
                preflight_fingerprint,
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
    self: AgentJobService,
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
    self: AgentJobService,
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
    self: AgentJobService, session: Session, operation: StoredOperation
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
    if job is None or not column_is_document(job, "result"):
        return
    if "cancel_requested" not in column_value(job, "result"):
        return
    if isinstance(column_field(job, "result", "cancel_requested"), bool):
        return
    self._write_refusal_note(
        session,
        operation=None,
        job_id=job.id,
        reason=_claim_note_reason("parent-cancel-flag-malformed", kind=operation.kind),
    )


def _refresh_parent_claim_refusal(
    session: Session, operation: StoredOperation, now: datetime
) -> None:
    """Replace a recovered claim's stale note with a live sibling's cause."""

    job = session.get(Job, operation.parent_job_id)
    if (
        job is None
        or not isinstance(job.status_reason, str)
        or not job.status_reason.startswith(_CLAIM_REFUSAL_PREFIX)
    ):
        return
    sibling_reason = session.scalar(
        select(StoredOperation.status_reason)
        .where(
            StoredOperation.parent_job_id == job.id,
            StoredOperation.id != operation.id,
            StoredOperation.state.not_in(_CONCLUDED_OUTCOMES),
            StoredOperation.status_reason.is_not(None),
        )
        .order_by(StoredOperation.node_id, StoredOperation.id)
        .limit(1)
    )
    if job.status_reason != sibling_reason:
        job.status_reason = sibling_reason
        job.updated_at = now


def record_boundary_refusal(
    self: AgentJobService,
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
    when the fence names the operation's current attempt, so a replayed old
    boundary cannot annotate newer work.  The parent job
    receives the same note on the same surface the jobs API returns.
    """

    prefixes = {
        "heartbeat": "heartbeat refused: ",
        "result": "result refused: ",
    }
    prefix = prefixes.get(boundary)
    if prefix is None:
        raise InvalidValue(
            "agent boundary is invalid", reason=InvalidRequestReason.MALFORMED
        )
    with self._sessions.begin() as session:
        current = session.scalar(
            select(AgentOperationAttempt).where(AgentOperationAttempt.fence == fence)
        )
        if current is None:
            return False
        operation = session.scalar(
            select(StoredOperation)
            .where(StoredOperation.id == current.operation_id)
            .with_for_update(of=StoredOperation)
        )
        if operation is None or operation.current_attempt != current.attempt:
            return False
        reason = _refusal_reason(prefix, check, attempt=current.attempt, **facts)
        self._write_refusal_note(
            session,
            operation=operation,
            job_id=operation.parent_job_id,
            reason=reason,
        )
        return True


def _excluded_work_refusal(
    self: AgentJobService,
    session: Session,
    node: AgentNode,
    now: datetime,
) -> tuple[StoredOperation, str, ClaimFacts] | None:
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
            StoredOperation.state.in_(agent_operation_states.LIVE),
        )
        .order_by(StoredOperation.created_at, StoredOperation.id)
        .limit(1)
    )
    if operation is None:
        return None
    facts = ClaimFacts(kind=operation.kind, state=operation.state)
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
            facts.model_copy(
                update=_claim_condition_facts(
                    condition.check, operation, attempt, node, now
                ).model_dump(exclude_none=True)
            ),
        )
    # Every modelled condition held, so the operation is claimable now. The
    # claim query that found nothing ran earlier in this transaction, and
    # READ COMMITTED lets work enqueued (or released) since then show up
    # here: the node simply polled a moment too soon and claims it on its
    # next poll. That is not a refusal, and recording one would show the
    # operator a false "claim refused" on a healthy start.
    if (
        session.scalar(
            self._claimable_operations(node.node_id, now).with_only_columns(
                StoredOperation.id
            )
        )
        is not None
    ):
        return None
    # The query still finds nothing although every modelled condition held.
    # The predicate is built from the conditions just evaluated, so this is
    # a defensive last resort, not a path any real operation takes.
    return (
        operation,
        "unclassified-unclaimable",
        facts.model_copy(update={"attempt": operation.current_attempt}),
    )
