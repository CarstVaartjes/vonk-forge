"""Ending assertions shared by service fixtures, with real SQL hold inspection.

World fixtures expose a Session (or session factory) as ``sessions``. Small
in-memory worlds may expose ``holds`` with explicit owner state instead.
Nothing is mutated by hold inspection; missing owners and operator waits fail.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_control.models import (
    AgentOperation,
    AgentOperationAttempt,
    ArtifactLifecycleGate,
    Job,
    JobAttempt,
    ModelCacheOperation,
    ResourceReservation,
)
from vonk_control.reservation_owners import reservation_owner

NON_BLOCKING_ENDS = frozenset({"cancelled", "superseded", "succeeded", "failed"})
OPERATOR_WAIT_STATES = frozenset({"needs-operator", "waiting-for-operator"})
ADMITTED_STATES = frozenset(
    {"queued", "running", "observing", "backoff", "succeeded", "pending", "accepted"}
)


@dataclass(frozen=True)
class Hold:
    kind: str
    owner_id: str | None
    owner_state: str | None


class Operation(Protocol):
    @property
    def state(self) -> str: ...


class MemoryWorld(Protocol):
    holds: Iterable[Hold]


def _assert_holds(holds: Iterable[Hold]) -> None:
    for hold in holds:
        assert hold.owner_id and hold.owner_state, f"orphaned {hold.kind}: owner absent"
        assert hold.owner_state not in NON_BLOCKING_ENDS | OPERATOR_WAIT_STATES, (
            f"orphaned {hold.kind}: {hold.owner_id} is {hold.owner_state}"
        )


def _assert_session(session: Session) -> None:
    removal_owner: ModelCacheOperation | Job | None
    for gate in session.scalars(
        select(ArtifactLifecycleGate).where(
            ArtifactLifecycleGate.removal_owner_id.is_not(None)
        )
    ):
        if gate.removal_owner_kind == "model-cache-operation":
            removal_owner = session.get(ModelCacheOperation, gate.removal_owner_id)
        elif gate.removal_owner_kind == "recipe-image-job":
            removal_owner = session.get(Job, gate.removal_owner_id)
        else:
            raise AssertionError(
                f"unknown removal gate owner: {gate.removal_owner_kind}"
            )
        _assert_holds(
            [
                Hold(
                    "removal gate",
                    gate.removal_owner_id,
                    removal_owner.state if removal_owner else None,
                )
            ]
        )
    for reservation in session.scalars(
        select(ResourceReservation).where(
            ResourceReservation.state.in_(["active", "promised"])
        )
    ):
        owner = reservation_owner(session, reservation.owner_kind, reservation.owner_id)
        assert owner.state not in {"missing", "unknown"}, (
            f"unresolved reservation owner: {owner.describe()}"
        )
        assert not owner.dead, f"orphaned reservation: {owner.describe()}"
        assert owner.state not in NON_BLOCKING_ENDS | OPERATOR_WAIT_STATES, (
            f"ended reservation owner: {owner.describe()}"
        )
    for operation in session.scalars(
        select(ModelCacheOperation).where(
            ModelCacheOperation.lease_deadline.is_not(None)
        )
    ):
        _assert_holds([Hold("cache claim/lease", operation.id, operation.state)])
    for attempt in session.scalars(
        select(JobAttempt).where(
            JobAttempt.state.in_(["running", "observing", "claimed"])
        )
    ):
        job = session.get(Job, attempt.job_id)
        _assert_holds(
            [Hold("job claim/lease", attempt.job_id, job.state if job else None)]
        )
    for attempt in session.scalars(
        select(AgentOperationAttempt).where(
            AgentOperationAttempt.state.in_(
                ["running", "observing", "claimed", "dispatched"]
            )
        )
    ):
        agent_operation = session.get(AgentOperation, attempt.operation_id)
        _assert_holds(
            [
                Hold(
                    "agent claim/lease",
                    attempt.operation_id,
                    agent_operation.state if agent_operation else None,
                )
            ]
        )


def assert_no_orphaned_holds(session_or_world: Session | MemoryWorld | object) -> None:
    if isinstance(session_or_world, Session):
        _assert_session(session_or_world)
        return
    holds = getattr(session_or_world, "holds", None)
    if holds is not None:
        _assert_holds(holds)
        return
    sessions = getattr(session_or_world, "sessions", None)
    assert sessions is not None, (
        "world must expose sessions or an explicit hold inventory"
    )
    if isinstance(sessions, Session):
        _assert_session(sessions)
    else:
        with sessions() as session:
            _assert_session(session)


def assert_ended_without_blocking[World, Receipt: Operation](
    world: World,
    operation: Receipt,
    *,
    end: Callable[[Receipt], Receipt],
    fresh: Callable[[World], Receipt],
    assert_released: Callable[[], None] | None = None,
    assert_reason: Callable[[Receipt], None] | None = None,
) -> tuple[Receipt, Receipt]:
    original_key = getattr(operation, "request_key", None) or getattr(
        operation, "request_id", None
    )
    assert original_key, "operation must carry a request key"
    ended = end(operation)
    assert ended.state not in OPERATOR_WAIT_STATES, "ending requires operator action"
    assert ended.state in NON_BLOCKING_ENDS, f"operation did not end: {ended.state}"
    if ended.state == "failed" and assert_reason is not None:
        assert_reason(ended)
    elif ended.state == "failed":
        codes = [getattr(ended, "reason_code", None)]
        codes.extend(
            getattr(blocker, "code", None) for blocker in getattr(ended, "blockers", [])
        )
        failure = getattr(ended, "failure", None)
        codes.append(getattr(failure, "code", None))
        assert any(codes), "failed ending requires a typed reason code"
    if assert_released is None:
        assert_no_orphaned_holds(world)
    else:
        assert_released()
    admitted = fresh(world)
    fresh_key = getattr(admitted, "request_key", None) or getattr(
        admitted, "request_id", None
    )
    assert fresh_key and fresh_key != original_key, (
        "fresh operation must have a new request key"
    )
    assert admitted.state in ADMITTED_STATES, (
        f"fresh operation refused: {admitted.state}"
    )
    refusal = getattr(admitted, "refusal", None)
    assert not refusal, f"fresh operation refused: {refusal}"
    if assert_released is None:
        assert_no_orphaned_holds(world)
    else:
        assert_released()
    return ended, admitted
