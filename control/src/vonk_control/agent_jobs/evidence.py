"""Evidence for the node-scoped agent queue."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import String, cast, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentOperation,
    LifecycleState,
    RecipeStopResult,
    RunState,
    UnknownOutcomeError,
    canonical_message,
    validate_result_for_operation,
)
from vonk_agent_protocol.contracts import AgentFailureResult
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_agent_protocol.recipe_operations import RecipeStartPayload, RecipeStopPayload

from .. import agent_operation_states
from ..agent_operation_facts import aware as _aware
from ..models import AgentNode, AgentOperationAttempt, Job, RecipeRun, RunNode
from ..models import AgentOperation as StoredOperation
from ..operation_contract import sanitize_failure_evidence
from ..recovery_policy import FailureKind
from .contracts import _REFUSAL_PREFIXES
from .stored import column_field, column_message, column_value


def _failure_result(
    error_code: str,
    reason: str,
    *,
    uncertain: bool,
    failure_kind: FailureKind | None = None,
) -> AgentFailureResult:
    """Build a typed failure result through the redaction boundary."""

    if failure_kind is None:
        failure_kind = (
            FailureKind.UNCERTAIN_EFFECT if uncertain else FailureKind.INVALID_CONTRACT
        )
    # The reason is redacted and bounded before the typed result is built, so a
    # long or sensitive reason cannot make the failure itself unrepresentable.
    evidence = {
        "error_code": error_code,
        "summary": reason,
        "reason": reason,
        "uncertain": uncertain,
        "recovery": "inspect-before-resume" if uncertain else "retry-or-inspect",
        "failure_kind": failure_kind.value,
        **({} if uncertain else {"status": "failed"}),
    }
    return AgentFailureResult.model_validate(sanitize_failure_evidence(evidence))


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


def _is_refusal_reason(reason: str | None) -> bool:
    return reason is None or reason.startswith(_REFUSAL_PREFIXES)


def _exact_service_stop_receipt_covers_start(
    session: Session, source: StoredOperation, current_ordinal: int
) -> bool:
    """Observe an older Start's exact runtime through a complete physical Stop.

    The Start and its attempt remain historical evidence. Only the same frozen
    runtime generation is resolved; cancellation or a terminal parent alone
    proves nothing. Current node intent still fences every original Start claim.
    """
    from ..job_documents import RecipeStartParent, RecipeStopParent
    from ..recipe_stop_payloads import (
        durable_run_stop_payloads,
        stop_payload_from_start,
    )

    if (
        source.kind != AgentOperation.RECIPE_START.value
        or source.current_attempt < 1
        or source.workload_intent_ordinal is None
        or source.workload_intent_ordinal >= current_ordinal
        or source.payload_digest
        != hashlib.sha256(column_message(source, "payload")).hexdigest()
    ):
        return False
    source_parent = session.get(Job, source.parent_job_id)
    source_attempt = session.scalar(
        select(AgentOperationAttempt).where(
            AgentOperationAttempt.operation_id == source.id,
            AgentOperationAttempt.attempt == source.current_attempt,
        )
    )
    node = session.get(AgentNode, source.node_id)
    if (
        source_parent is None
        or source_parent.kind != "recipe.start"
        or source.authority_revision != source_parent.authority_revision
        or source_attempt is None
        or node is None
        or node.workload_intent_ordinal != current_ordinal
        or source_parent.payload_digest
        != hashlib.sha256(column_message(source_parent, "payload")).hexdigest()
    ):
        return False
    try:
        start = column_value(source, "payload")
        parent = column_value(source_parent, "payload")
        if not isinstance(start, RecipeStartPayload) or not isinstance(
            parent, RecipeStartParent
        ):
            return False
        run = session.get(RecipeRun, start.run_id)
        if (
            parent.owner_kind != "run"
            or parent.owner_id != start.run_id
            or parent.plan_digest != start.plan_digest
            or parent.workload_intent_ordinal != source.workload_intent_ordinal
            or run is None
            or run.state != RunState.STOPPED
            or run.run_generation != start.run_generation
        ):
            return False
        members = tuple(
            session.scalars(select(RunNode).where(RunNode.run_id == run.id))
        )
        member_ids = {member.node_id for member in members}
        if (
            source.node_id not in member_ids
            or set(column_value(source_parent, "targets")) != member_ids
        ):
            return False
        accepted_sources = [
            item
            for phase in parent.phases or []
            for item in phase
            if item.node_id == source.node_id
            and item.operation_id == source.id
            and canonical_message(item.payload) == canonical_message(start)
        ]
        if len(accepted_sources) != 1:
            return False
        expected = durable_run_stop_payloads(
            session,
            run,
            members,
            run_generation=start.run_generation,
            cancel_pending_start=True,
            allow_missing_nodes=False,
        )
        source_target = stop_payload_from_start(start, cancel_pending_start=True)
        source_target = source_target.model_copy(
            update={
                "stop_timeout_seconds": expected[source.node_id].stop_timeout_seconds
            }
        )
        if canonical_message(source_target) != canonical_message(
            expected[source.node_id]
        ):
            return False
        stop_parents = session.scalars(
            select(Job)
            .where(
                Job.kind == "recipe.stop",
                Job.state == LifecycleState.SUCCEEDED,
                Job.payload["owner_id"].as_string() == run.id,
                Job.id.in_(
                    select(StoredOperation.parent_job_id).where(
                        StoredOperation.node_id == source.node_id,
                        StoredOperation.kind == AgentOperation.RECIPE_STOP.value,
                        StoredOperation.state == LifecycleState.SUCCEEDED,
                        StoredOperation.workload_intent_ordinal
                        > source.workload_intent_ordinal,
                        StoredOperation.workload_intent_ordinal <= current_ordinal,
                        StoredOperation.payload["run_id"].as_string() == run.id,
                        cast(
                            StoredOperation.payload["run_generation"].as_string(),
                            String,
                        )
                        == str(start.run_generation),
                    )
                ),
            )
            .order_by(Job.created_at.desc(), Job.id)
        )
        for stop_parent in stop_parents:
            if (
                stop_parent.payload_digest
                != hashlib.sha256(column_message(stop_parent, "payload")).hexdigest()
            ):
                continue
            try:
                stop_document = column_value(stop_parent, "payload")
                if not isinstance(stop_document, RecipeStopParent):
                    continue
            except (TypeError, ValueError):
                continue
            ordinal = stop_document.workload_intent_ordinal
            if (
                stop_document.owner_kind != "run"
                or stop_document.owner_id != run.id
                or stop_document.phases is None
                or stop_document.profile_partial_stop is not None
                or stop_document.execution_mode is not None
                or stop_document.recovery is not None
                or ordinal is None
                or not source.workload_intent_ordinal < ordinal <= current_ordinal
                or set(column_value(stop_parent, "targets")) != member_ids
            ):
                continue
            children = tuple(
                session.scalars(
                    select(StoredOperation).where(
                        StoredOperation.parent_job_id == stop_parent.id
                    )
                )
            )
            if (
                len(children) != len(member_ids)
                or {child.node_id for child in children} != member_ids
            ):
                continue
            accepted_stops = [item for phase in stop_document.phases for item in phase]
            if (
                len(accepted_stops) != len(member_ids)
                or {item.node_id for item in accepted_stops} != member_ids
                or any(
                    not any(
                        child.id == item.operation_id and child.node_id == item.node_id
                        for child in children
                    )
                    or canonical_message(item.payload)
                    != canonical_message(expected[item.node_id])
                    for item in accepted_stops
                )
            ):
                continue
            proven = True
            for child in children:
                attempt = session.scalar(
                    select(AgentOperationAttempt).where(
                        AgentOperationAttempt.operation_id == child.id,
                        AgentOperationAttempt.attempt == child.current_attempt,
                    )
                )
                if (
                    child.kind != AgentOperation.RECIPE_STOP.value
                    or child.state != LifecycleState.SUCCEEDED
                    or child.current_attempt < 1
                    or child.workload_intent_ordinal != ordinal
                    or child.authority_revision != stop_parent.authority_revision
                    or child.created_at < source.created_at
                    or attempt is None
                    or attempt.state != LifecycleState.SUCCEEDED
                    or child.payload_digest
                    != hashlib.sha256(column_message(child, "payload")).hexdigest()
                    or column_message(child, "payload")
                    != canonical_message(expected[child.node_id])
                ):
                    proven = False
                    break
                try:
                    receipt = validate_result_for_operation(
                        child.kind,
                        column_value(attempt, "result"),
                        state=agent_operation_states.attempt_wire_state(attempt),
                    )
                except (TypeError, ValueError):
                    proven = False
                    break
                if not isinstance(receipt, RecipeStopResult):
                    proven = False
                    break
            if proven:
                return True
    except (TypeError, ValueError, UnknownOutcomeError):
        return False
    return False


def _profile_stop_covers_jobrun_mutations(
    session: Session,
    current: StoredOperation,
    current_parent: Job,
    active_mutations: Sequence[StoredOperation],
    *,
    now: datetime,
) -> bool:
    """Prove this exact current profile Stop covers every older JobRun here."""

    if (
        current.kind != AgentOperation.RECIPE_STOP.value
        or current_parent.kind != "recipe.stop"
        or column_field(current_parent, "payload", "execution_mode")
        != "profile-jobrun-stop"
        or current_parent.payload_digest
        != hashlib.sha256(column_message(current_parent, "payload")).hexdigest()
    ):
        return False
    try:
        from ..profile_stop_authority import (
            ProfileJobRunStopJob,
            ProfileStopAuthorityError,
            validate_profile_jobrun_stop_target,
        )
        from ..recipe_stop_payloads import stop_payload_from_job_run

        parent = ProfileJobRunStopJob.model_validate_parent(
            column_value(current_parent, "payload")
        )
        authorization = parent.profile_stop_authorization
        current_stop = column_value(current, "payload")
        if not isinstance(current_stop, RecipeStopPayload):
            return False
        current_digest = hashlib.sha256(canonical_message(current_stop)).hexdigest()
        current_targets = [
            target
            for target in authorization.targets
            if target.node_id == current.node_id
            and target.stop_payload_sha256 == current_digest
        ]
        if len(current_targets) != 1:
            return False
        validate_profile_jobrun_stop_target(
            session,
            authorization,
            current_targets[0],
            current_stop,
            operation=current,
            stop_parent=current_parent,
            now=now,
            require_current=True,
        )
        for old in active_mutations:
            if (
                old.kind != AgentOperation.RECIPE_JOB_RUN.value
                or old.node_id != current.node_id
            ):
                return False
            matches = [
                target
                for target in authorization.targets
                if target.source_operation_id == old.id
                and target.source_job_id == old.parent_job_id
                and target.node_id == old.node_id
            ]
            if len(matches) != 1:
                return False
            request = column_value(old, "payload")
            if not isinstance(request, RecipeJobRunRequest):
                return False
            expected_stop = stop_payload_from_job_run(
                request, cancel_pending_start=True
            )
            validate_profile_jobrun_stop_target(
                session,
                authorization,
                matches[0],
                expected_stop,
                stop_parent=current_parent,
                now=now,
                require_current=True,
            )
        return True
    except (ProfileStopAuthorityError, TypeError, ValueError):
        return False
