"""Predicates for the node-scoped agent queue."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Boolean, and_, or_, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.functions import FunctionElement
from vonk_agent_protocol import AgentOperation, canonical_message
from vonk_agent_protocol.contracts import canonical_payload
from vonk_agent_protocol.recipe_operations import RecipeStartPayload

from .. import agent_operation_states
from ..agent_job_contract import ClaimFacts
from ..agent_operation_facts import aware as _aware
from ..agent_operation_facts import (
    operation_start_deadline as _operation_start_deadline,
)
from ..logging import redact_text
from ..models import AgentNode, AgentOperationAttempt, Job
from ..models import AgentOperation as StoredOperation
from ..stored_json import read_row_column
from .contracts import (
    _CLAIM_NOTE_PREFIX,
    _CLAIM_REFUSAL_PREFIX,
    _MAX_CLAIM_REFUSAL_REASON,
)


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


def _document(value: BaseModel) -> Any:
    """Return the protocol's validated, deterministic JSON representation."""
    return json.loads(canonical_message(value))


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

    states: tuple[str, ...]
    conditions: tuple[_ClaimCondition, ...]

    @property
    def expression(self) -> ColumnElement[bool]:
        return and_(
            StoredOperation.state.in_(self.states),
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
            if state in branch.states:
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
                agent_operation_states.sql_attempt_failed_or_unknown(
                    AgentOperationAttempt
                ),
                and_(
                    agent_operation_states.sql_attempt_lapsed(AgentOperationAttempt),
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
                (agent_operation_states.QUEUED,),
                (
                    _ClaimCondition(
                        "queued-attempt-not-zero",
                        StoredOperation.current_attempt == 0,
                    ),
                ),
            ),
            _ClaimBranch(
                (agent_operation_states.RUNNING,),
                (
                    _ClaimCondition("running-attempt-missing", attempt_present),
                    _ClaimCondition("running-attempt-not-running", attempt_running),
                    _ClaimCondition("running-lease-live", attempt_lease_elapsed),
                ),
            ),
            _ClaimBranch(
                agent_operation_states.PARKED,
                (
                    _ClaimCondition(
                        "operator-retry-not-authorized",
                        # A schedule is the authorisation: the lifecycle core
                        # sets ``next_action_at`` for an automatic retry and for
                        # an operator's resume, and the claim clears it.
                        and_(
                            StoredOperation.next_action_at.is_not(None),
                            StoredOperation.observe_count == 0,
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
                            StoredOperation.next_action_at <= now,
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


def _anchor_start_budget(
    session: Session, operation: StoredOperation, now: datetime
) -> None:
    """Begin a queued distributed start's budget when it is first dispatched.

    The Controller binds ``start_deadline`` when it queues the start, but an
    order may legitimately wait before any agent can run it: a mutating
    operation on the same Spark (a long transfer or install) holds the node, so
    the start is claimed only afterwards.  Time spent queued is not time spent
    launching, so the first claim of the job's first order moves every unclaimed
    deadline of the job to ``now + budget`` (the budget being the span the
    Controller accepted at queue time).  The deadline is only ever extended, is
    set once per job, and an exact-recovery start keeps its own recovery
    deadline.  Later phases are issued from the job's stored phases, so they
    carry the anchored deadline too.
    """

    # job_documents reaches this module through the build contracts.
    from ..job_documents import RecipeStartParent

    job = session.get(Job, operation.parent_job_id, with_for_update=True)
    if job is None:
        return
    parent = read_row_column(job, "payload")
    if not isinstance(parent, RecipeStartParent):
        # Anchoring is a courtesy; a parent that cannot be read keeps its
        # queue-time deadlines (its own reader retires it).
        return
    if parent.recovery is not None or parent.start_anchored_at is not None:
        return
    queued_deadline = _operation_start_deadline(operation)
    if queued_deadline is None:
        return
    budget = _aware(queued_deadline) - _aware(operation.created_at)
    if budget <= timedelta(0):
        return
    anchored = (_aware(now) + budget).isoformat()
    if _aware(now) + budget <= _aware(queued_deadline):
        # Nothing waited: the queue-time deadline already is the anchored one.
        return

    def rebound(payload: RecipeStartPayload) -> RecipeStartPayload:
        return payload.model_copy(update={"start_deadline": anchored})

    for sibling in session.scalars(
        select(StoredOperation)
        .where(
            StoredOperation.parent_job_id == job.id,
            StoredOperation.kind == AgentOperation.RECIPE_START.value,
            StoredOperation.current_attempt == 0,
        )
        .with_for_update(of=StoredOperation)
    ):
        queued = read_row_column(sibling, "payload")
        if not isinstance(queued, RecipeStartPayload) or queued.start_deadline is None:
            continue
        document = rebound(queued)
        sibling.payload = json.loads(canonical_message(document))
        sibling.payload_digest = hashlib.sha256(
            canonical_payload(AgentOperation(sibling.kind), document)
        ).hexdigest()
    updated = parent.model_copy(
        update={
            "start_anchored_at": _aware(now),
            **(
                {"start_deadline": datetime.fromisoformat(anchored)}
                if parent.start_deadline is not None
                else {}
            ),
            "phases": None
            if parent.phases is None
            else [
                [
                    item.model_copy(update={"payload": rebound(item.payload)})
                    if item.payload.start_deadline is not None
                    else item
                    for item in group
                ]
                for group in parent.phases
            ],
        }
    )
    job.payload = json.loads(canonical_message(updated))
    job.payload_digest = hashlib.sha256(canonical_message(updated)).hexdigest()


def _claim_condition_facts(
    check: str,
    operation: StoredOperation,
    attempt: AgentOperationAttempt | None,
    node: AgentNode,
    now: datetime,
) -> ClaimFacts:
    """Return the bounded facts that explain one failed named condition."""

    if check == "workload-intent-superseded":
        return ClaimFacts(
            operation_intent=operation.workload_intent_ordinal,
            node_intent=node.workload_intent_ordinal,
        )
    if check in {
        "queued-attempt-not-zero",
        "operator-retry-not-authorized",
        "operator-retry-attempt-not-ready",
    }:
        return ClaimFacts(attempt=operation.current_attempt)
    if check in {"running-attempt-missing", "running-attempt-not-running"}:
        return ClaimFacts(
            attempt=operation.current_attempt,
            attempt_state=None if attempt is None else attempt.state,
        )
    if check in {"running-lease-live", "upgrade-safety-not-elapsed"}:
        return ClaimFacts(
            attempt=operation.current_attempt,
            lease_deadline=None
            if attempt is None
            else _aware(attempt.lease_deadline).isoformat(),
        )
    if check == "operator-retry-not-due":
        return ClaimFacts(
            attempt=operation.current_attempt,
            retry_due_at=None
            if operation.next_action_at is None
            else _aware(operation.next_action_at).isoformat(),
        )
    return ClaimFacts()


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
