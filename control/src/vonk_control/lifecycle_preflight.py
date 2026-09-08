"""Durable, recipe-bound admission probes without replaying expensive phases."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select
from vonk_agent_protocol.runtime_preflight import RuntimePreflightResult

from .models import AgentNode, AgentOperation, AgentOperationAttempt, Job
from .runtime_preflight import (
    admission_blockers,
    latest_result,
    node_fingerprint,
    recipe_requirements,
    request_digest,
)
from .strict_json import StrictJSONModel

NodeId = Annotated[str, StringConstraints(pattern=r"^spk_[0-9a-f]{32}$")]
UuidId = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]


class LifecyclePreflightCheckpoint(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    phase_index: int = Field(ge=0, le=31)
    pending_job_id: UuidId | None = None
    pending_node_id: NodeId | None = None
    attempts: dict[NodeId, Annotated[int, Field(ge=1, le=3)]] = Field(
        default_factory=dict, max_length=33
    )
    receipts: dict[NodeId, RuntimePreflightResult] = Field(
        default_factory=dict, max_length=33
    )

    @model_validator(mode="after")
    def pending_identity_is_complete(self):
        if (self.pending_job_id is None) != (self.pending_node_id is None):
            raise ValueError(
                "preflight pending child and node identities must be paired"
            )
        return self


class LifecyclePreflight:
    def __init__(self, sessions, queue, clock, minimum_free_bytes: int) -> None:
        self._sessions = sessions
        self._queue = queue
        self._clock = clock
        self._minimum_free_bytes = minimum_free_bytes

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
        checkpoint = (
            previous.model_copy(deep=True)
            if previous
            else LifecyclePreflightCheckpoint(phase_index=phase_index)
        )
        if checkpoint.phase_index != phase_index:
            checkpoint = LifecyclePreflightCheckpoint(
                phase_index=phase_index, receipts=checkpoint.receipts
            )
        with self._sessions.begin() as session:
            # Consume the outstanding probe first, then recheck every other node.
            # A previously checked rank can change while its peer is probing.
            ordered_nodes = sorted(
                nodes.items(), key=lambda item: (item[0] != checkpoint.pending_node_id, item[0])
            )
            for node_id, source_build in ordered_nodes:
                node = session.get(AgentNode, node_id, with_for_update=True)
                if node is None:
                    return checkpoint, "runtime_preflight.node_missing"
                request = recipe_requirements(
                    document,
                    source_build=source_build,
                    minimum_free_bytes=self._minimum_free_bytes,
                )
                fingerprint = node_fingerprint(node.capabilities)
                result = checkpoint.receipts.get(node_id) or latest_result(
                    session, node_id, requirements_sha256=request_digest(request)
                )
                blockers = admission_blockers(
                    request,
                    result,
                    current_fingerprint=fingerprint,
                    now=int(now.timestamp()),
                )
                if not blockers and checkpoint.pending_node_id != node_id:
                    checkpoint.receipts[node_id] = result
                    continue
                if checkpoint.pending_job_id:
                    if checkpoint.pending_node_id != node_id:
                        continue
                    child = session.get(Job, checkpoint.pending_job_id)
                    if child is None:
                        return checkpoint, "runtime_preflight.child_missing"
                    if child.state in {"queued", "running"}:
                        return checkpoint, None
                    if child.state != "succeeded":
                        return (
                            checkpoint,
                            child.status_reason or "runtime_preflight.execution_failed",
                        )
                    operation = session.scalar(
                        select(AgentOperation).where(
                            AgentOperation.parent_job_id == child.id,
                            AgentOperation.node_id == node_id,
                        )
                    )
                    raw = (
                        session.scalar(
                            select(AgentOperationAttempt.result).where(
                                AgentOperationAttempt.operation_id == operation.id,
                                AgentOperationAttempt.attempt
                                == operation.current_attempt,
                            )
                        )
                        if operation
                        else None
                    )
                    if raw is None:
                        return checkpoint, "runtime_preflight.receipt_missing"
                    result = RuntimePreflightResult.model_validate(raw)
                    checkpoint.receipts[node_id] = result
                    checkpoint.pending_job_id = None
                    checkpoint.pending_node_id = None
                    blockers = admission_blockers(
                        request,
                        result,
                        current_fingerprint=fingerprint,
                        now=int(now.timestamp()),
                    )
                    if not blockers:
                        continue
                    if any(
                        item.code
                        not in {
                            "runtime_preflight.host_changed",
                            "runtime_preflight.requirements_changed",
                            "runtime_preflight.stale",
                        }
                        for item in blockers
                    ):
                        return checkpoint, "; ".join(item.detail for item in blockers)[
                            :512
                        ]
                attempt = checkpoint.attempts.get(node_id, 0) + 1
                if attempt > 3:
                    return checkpoint, "runtime_preflight.retry_exhausted"
                checkpoint.attempts[node_id] = attempt
                key = str(
                    uuid.uuid5(
                        uuid.UUID(request_key),
                        f"runtime-preflight:{phase_index}:{node_id}:{attempt}",
                    )
                )
                child = session.scalar(select(Job).where(Job.request_id == key))
                if child is None:
                    digest = request_digest(request)
                    payload = request.model_dump(mode="json")
                    child = Job(
                        id=key,
                        request_id=key,
                        kind="runtime.preflight.v1",
                        state="running",
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
                        "runtime.preflight.v1",
                        digest,
                        payload,
                        operation_id=str(uuid.uuid5(uuid.UUID(key), "probe")),
                    )
                checkpoint.pending_job_id = child.id
                checkpoint.pending_node_id = node_id
                break
        if checkpoint.pending_job_id:
            self._queue.notify_available()
        return checkpoint, None
