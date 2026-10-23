"""Durable, recipe-bound admission probes without replaying expensive phases."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    StringConstraints,
)
from sqlalchemy import select
from vonk_agent_protocol import AgentOperation as OperationKind
from vonk_agent_protocol import (
    LifecycleState,
    OperationReason,
    RuntimePreflightCode,
    canonical_message,
)
from vonk_agent_protocol.runtime_preflight import RuntimePreflightResult

from .failure_classification import is_security_failure
from .inventory_items import inventory_items
from .inventory_items_contract import PreflightInventoryItem
from .lifecycle.job import JobAdapter
from .models import AgentNode, AgentOperation, AgentOperationAttempt, Job
from .persisted_waits import typed_wait_reason
from .recovery_policy import RECOVERY
from .runtime_preflight import (
    admission_blockers,
    latest_result,
    recipe_requirements,
    request_digest,
)
from .strict_json import StrictJSONModel

NodeId = Annotated[str, StringConstraints(pattern=r"^spk_[0-9a-f]{32}$")]
# The agent bounds probe execution. After its normal delivery/reporting window,
# observe less frequently while that same fenced operation owns recovery.
PENDING_PROBE_NORMAL_WINDOW = timedelta(seconds=180)
PROBE_OBSERVATION_INTERVAL = timedelta(seconds=5)
DELAYED_PROBE_OBSERVATION_INTERVAL = timedelta(seconds=60)
UuidId = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]


class LifecyclePreflightCheckpoint(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    phase_index: int = Field(ge=0, le=31)
    pending_jobs: dict[NodeId, UuidId] = Field(default_factory=dict)
    retry_at: dict[NodeId, AwareDatetime] = Field(default_factory=dict)
    next_check_at: AwareDatetime | None = None
    last_failure_code: OperationReason | None = None
    last_failure_detail: str | None = None
    attempts: dict[NodeId, Annotated[int, Field(ge=1)]] = Field(default_factory=dict)
    receipts: dict[NodeId, PreflightInventoryItem] = Field(default_factory=dict)


# Security refusals (failure_classification.is_security_failure) park the
# preflight; everything else is transient and retried with backoff.
def _child_error_code(result: object) -> str | None:
    """Read the typed failure code from a child attempt result, never free text."""
    if not isinstance(result, Mapping):
        return None
    for key in ("error_code", "helper_error_code"):
        value = result.get(key)
        if isinstance(value, str) and is_security_failure(value):
            return value
    value = result.get("error_code")
    return value if isinstance(value, str) else None


class LifecyclePreflight:
    def __init__(self, sessions, queue, clock, minimum_free_bytes: int) -> None:
        self._sessions = sessions
        self._queue = queue
        self._clock = clock
        self._minimum_free_bytes = minimum_free_bytes
        self._recovery = RECOVERY

    def ensure(
        self,
        *,
        document: Mapping[str, object],
        nodes: Mapping[str, bool],
        phase_index: int,
        request_key: str,
        actor: str,
        previous: LifecyclePreflightCheckpoint | None,
    ) -> tuple[LifecyclePreflightCheckpoint, str | None]:
        """Return one bounded checkpoint and an optional blocking reason.

        Successful probe execution means observations exist; each mandatory
        finding must also pass for the current request, fingerprint and age.
        """
        now: datetime = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        checkpoint = (
            previous.model_copy(deep=True)
            if previous
            else LifecyclePreflightCheckpoint(phase_index=phase_index)
        )
        if checkpoint.phase_index != phase_index:
            checkpoint = LifecyclePreflightCheckpoint(
                phase_index=phase_index, receipts=checkpoint.receipts
            )

        checkpoint.next_check_at = None
        due: list[datetime] = []
        with self._sessions.begin() as session:
            # Pending child identities bind one probe round to this request and
            # phase. Consume its receipts as a set at their last arrival, rather
            # than ageing them by time spent waiting for the worker. The normal
            # freshness bound still limits the span between rank receipts;
            # after the round, reusable inventory is checked against now.
            round_active = bool(checkpoint.pending_jobs)
            round_receipts = list(checkpoint.receipts.values())
            for node_id, child_id in checkpoint.pending_jobs.items():
                if node_id not in nodes:
                    continue
                child = session.get(Job, child_id)
                if child is not None and child.state == LifecycleState.SUCCEEDED:
                    receipt = latest_result(
                        session, node_id, requirements_sha256=child.authority_revision
                    )
                    if receipt is not None:
                        round_receipts.append(receipt)
            round_received_at = max(
                (
                    receipt.received_at.timestamp()
                    for receipt in round_receipts
                    if receipt.received_at is not None
                ),
                default=now.timestamp(),
            )
            for node_id, source_build in sorted(nodes.items()):
                node = session.get(AgentNode, node_id, with_for_update=True)
                if node is None:
                    return checkpoint, RuntimePreflightCode.NODE_MISSING
                if node.revoked_at is not None:
                    return checkpoint, RuntimePreflightCode.NODE_REVOKED
                request = recipe_requirements(
                    document,
                    source_build=source_build,
                    minimum_free_bytes=self._minimum_free_bytes,
                )
                digest = request_digest(request)
                fingerprint = inventory_items(node).preflight_fingerprint

                def key(attempt: int, node_id: str = node_id) -> str:
                    return str(
                        uuid.uuid5(
                            uuid.UUID(request_key),
                            f"runtime-preflight:{phase_index}:{node_id}:{attempt}",
                        )
                    )

                # Checkpoints are derived. Adopt every already-issued child by
                # its deterministic identity, including after checkpoint loss.
                attempt = checkpoint.attempts.get(node_id, 0)
                while child := session.scalar(
                    select(Job).where(Job.request_id == key(attempt + 1))
                ):
                    attempt += 1
                    checkpoint.pending_jobs[node_id] = child.id
                if attempt:
                    checkpoint.attempts[node_id] = attempt
                receipt = latest_result(session, node_id, requirements_sha256=digest)
                child_id = checkpoint.pending_jobs.get(node_id)
                if child_id is not None:
                    child = session.get(Job, child_id)
                    operation = session.scalar(
                        select(AgentOperation).where(
                            AgentOperation.parent_job_id == child_id,
                            AgentOperation.node_id == node_id,
                        )
                    )
                    failure = None
                    error_code = None
                    if child is None:
                        failure = RuntimePreflightCode.CHILD_MISSING
                    elif operation is None:
                        failure = RuntimePreflightCode.OPERATION_MISSING
                    elif child.state in {LifecycleState.QUEUED, LifecycleState.RUNNING}:
                        created = child.created_at
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=UTC)
                        interval = (
                            DELAYED_PROBE_OBSERVATION_INTERVAL
                            if now >= created + PENDING_PROBE_NORMAL_WINDOW
                            else PROBE_OBSERVATION_INTERVAL
                        )
                        due.append(now + interval)
                        continue
                    else:
                        raw = session.scalar(
                            select(AgentOperationAttempt.result).where(
                                AgentOperationAttempt.operation_id == operation.id,
                                AgentOperationAttempt.attempt
                                == operation.current_attempt,
                            )
                        )
                        if child.state != LifecycleState.SUCCEEDED:
                            error_code = _child_error_code(raw)
                            failure = (
                                child.status_reason
                                or RuntimePreflightCode.EXECUTION_FAILED
                            )
                        elif raw is None:
                            failure = RuntimePreflightCode.RECEIPT_MISSING
                        else:
                            try:
                                result = RuntimePreflightResult.model_validate_json(
                                    canonical_message(raw)
                                )
                                if receipt is None or receipt.result != result:
                                    failure = RuntimePreflightCode.RECEIPT_MISSING
                            except (TypeError, ValueError):
                                failure = RuntimePreflightCode.RECEIPT_INVALID
                    checkpoint.pending_jobs.pop(node_id)
                    if failure is not None:
                        checkpoint.receipts.pop(node_id, None)
                        checkpoint.last_failure_code = (
                            typed_wait_reason(
                                error_code, RuntimePreflightCode.EXECUTION_FAILED
                            )
                            if error_code
                            else typed_wait_reason(
                                str(failure), RuntimePreflightCode.EXECUTION_FAILED
                            )
                        )
                        checkpoint.last_failure_detail = str(failure)
                        if is_security_failure(error_code):
                            return checkpoint, str(failure)
                        retry_at = self._recovery.next_attempt(
                            f"{request_key}:{phase_index}:{node_id}",
                            max(attempt, 1),
                            now,
                            ongoing_intent=True,
                        )
                        assert retry_at is not None
                        checkpoint.retry_at[node_id] = retry_at
                blockers = admission_blockers(
                    request,
                    receipt,
                    current_fingerprint=fingerprint,
                    now=(
                        round_received_at
                        if round_active and (child_id is None or failure is None)
                        else now.timestamp()
                    ),
                )
                if receipt is not None and not blockers:
                    checkpoint.receipts[node_id] = receipt
                    checkpoint.retry_at.pop(node_id, None)
                    continue
                checkpoint.receipts.pop(node_id, None)
                if (
                    child_id is not None
                    and failure is None
                    and any(
                        item.code
                        not in {
                            RuntimePreflightCode.HOST_CHANGED,
                            RuntimePreflightCode.REQUIREMENTS_CHANGED,
                            RuntimePreflightCode.STALE,
                        }
                        for item in blockers
                    )
                ):
                    return checkpoint, "; ".join(item.detail for item in blockers)[:512]
                if child_id is not None and failure is None and blockers:
                    retry_at = self._recovery.next_attempt(
                        f"{request_key}:{phase_index}:{node_id}",
                        max(attempt, 1),
                        now,
                        ongoing_intent=True,
                    )
                    assert retry_at is not None
                    checkpoint.retry_at[node_id] = retry_at
                    checkpoint.last_failure_code = blockers[0].code
                    checkpoint.last_failure_detail = "; ".join(
                        item.detail for item in blockers
                    )[:512]
                if (
                    node_id in checkpoint.retry_at
                    and now < checkpoint.retry_at[node_id]
                ):
                    due.append(checkpoint.retry_at[node_id])
                    continue
                checkpoint.retry_at.pop(node_id, None)
                if blockers:
                    checkpoint.last_failure_code = blockers[0].code
                    checkpoint.last_failure_detail = "; ".join(
                        item.detail for item in blockers
                    )[:512]
                attempt += 1
                checkpoint.attempts[node_id] = attempt
                payload = request.model_dump(mode="json")
                child = JobAdapter.new_job(
                    state=LifecycleState.RUNNING.value,
                    id=str(uuid.uuid4()),
                    request_id=key(attempt),
                    kind=OperationKind.RUNTIME_PREFLIGHT.value,
                    actor=actor,
                    authority_revision=digest,
                    targets=[node_id],
                    payload_digest=digest,
                    payload=payload,
                    created_at=now,
                    updated_at=now,
                )
                session.add(child)
                session.flush()
                self._queue.enqueue_in_session(
                    session,
                    child.id,
                    node_id,
                    OperationKind.RUNTIME_PREFLIGHT.value,
                    digest,
                    payload,
                    operation_id=str(uuid.uuid4()),
                )
                checkpoint.pending_jobs[node_id] = child.id
                due.append(now + PROBE_OBSERVATION_INTERVAL)
        checkpoint.next_check_at = min(due, default=None)
        if checkpoint.pending_jobs:
            self._queue.notify_available()
        if not due:
            checkpoint.last_failure_code = None
            checkpoint.last_failure_detail = None
        return checkpoint, None
