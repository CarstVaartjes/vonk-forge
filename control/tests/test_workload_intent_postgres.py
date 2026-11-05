"""Workload intents are numbered by a fenced conditional UPDATE, read plainly.

Lock plan step 4: the intent ordinal of a Spark was numbered under FOR UPDATE on
its ``agent_nodes`` row, so every intent read and every numbering waited behind
(and blocked) agent traffic on that row. The ordinal is now written only by
``vonk_control.workload_intent`` as ``UPDATE ... WHERE ordinal = :seen``.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import Disposition, WaitReason
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.failure_classification import disposition, wait_reason
from vonk_control.models import AgentNode, Base
from vonk_control.recipe_operations.intent import (
    _intent_is_current,
    _shared_workload_intent,
)
from vonk_control.workload_intent import (
    WorkloadIntentUnavailable,
    allocate_workload_intent,
    raise_workload_intent,
    take_workload_intent,
)

NODES = ("spk_" + "a" * 32, "spk_" + "b" * 32)
INTENTS = 8


@pytest.fixture
def sessions(postgres_engine):
    Base.metadata.create_all(postgres_engine)
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    with factory.begin() as session:
        for node in NODES:
            session.add(
                AgentNode(
                    node_id=node,
                    state=NodeIdentityState.ACTIVE,
                    workload_intent_ordinal=3,
                )
            )
    return factory


def _ordinals(sessions) -> dict[str, int]:
    with sessions() as session:
        return {
            node.node_id: node.workload_intent_ordinal
            for node in session.scalars(select(AgentNode))
        }


def test_concurrent_intents_on_one_node_get_distinct_increasing_ordinals(
    sessions, postgres_engine
):
    """Fails on main: there is no allocator; the old one read max() under FOR UPDATE."""
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", record)
    taken: list[int] = []
    guard = threading.Lock()
    start = threading.Barrier(INTENTS)
    errors: list[BaseException] = []

    def allocate() -> None:
        try:
            start.wait(timeout=30)
            with sessions.begin() as session:
                session.execute(text("SET LOCAL lock_timeout = '20s'"))
                ordinal = allocate_workload_intent(session, NODES[:1])
            with guard:
                taken.append(ordinal)
        except BaseException as error:  # noqa: BLE001 - reported to the test thread
            errors.append(error)

    threads = [threading.Thread(target=allocate) for _ in range(INTENTS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    event.remove(postgres_engine, "before_cursor_execute", record)
    assert not errors
    assert sorted(taken) == list(range(4, 4 + INTENTS))
    assert _ordinals(sessions)[NODES[0]] == 3 + INTENTS
    assert _ordinals(sessions)[NODES[1]] == 3
    assert not [s for s in statements if "FOR UPDATE" in s.upper()]
    with sessions.begin() as session:
        assert allocate_workload_intent(session, NODES[:1]) == 4 + INTENTS


def test_intent_reads_do_not_wait_for_a_held_agent_node_row(sessions, postgres_engine):
    """Fails on main: both reads took FOR UPDATE and waited behind the row's holder."""
    with postgres_engine.connect() as holder:
        holder.execute(
            text("SELECT 1 FROM agent_nodes WHERE node_id = ANY(:ids) FOR UPDATE"),
            {"ids": list(NODES)},
        )
        with sessions.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '300ms'"))
            assert _intent_is_current(session, 3, NODES)
            assert not _intent_is_current(session, 2, NODES)
            assert _shared_workload_intent(session, NODES) == 3
        holder.rollback()


def test_a_numbering_that_lost_the_race_changes_nothing(sessions):
    with sessions.begin() as session:
        seen = {node: 3 for node in NODES}
        newer = allocate_workload_intent(session, NODES[:1])
    assert newer == 4
    with sessions.begin() as session:
        ordinal, taken = take_workload_intent(session, seen)
    assert ordinal == 4
    assert taken == (NODES[1],)
    assert _ordinals(sessions) == {NODES[0]: 4, NODES[1]: 4}


def test_raising_an_intent_never_lowers_one(sessions):
    with sessions.begin() as session:
        allocate_workload_intent(session, NODES[:1])
        raise_workload_intent(session, NODES, 3)
        raise_workload_intent(session, NODES, 6)
        raise_workload_intent(session, NODES, 5)
    assert _ordinals(sessions) == {NODES[0]: 6, NODES[1]: 6}


def test_allocation_updates_the_nodes_already_loaded_in_the_session(sessions):
    with sessions.begin() as session:
        nodes = list(session.scalars(select(AgentNode).order_by(AgentNode.node_id)))
        ordinal = allocate_workload_intent(session, NODES)
        assert [node.workload_intent_ordinal for node in nodes] == [ordinal] * 2


def test_a_workload_intent_that_cannot_be_numbered_is_a_typed_wait():
    error = WorkloadIntentUnavailable("kept changing owner")
    assert disposition(error) is Disposition.WAIT
    assert wait_reason(error) is WaitReason.SCOPE_CHANGED


def test_a_revoked_spark_is_not_numbered(sessions):
    with sessions.begin() as session:
        session.get(AgentNode, NODES[1]).revoked_at = datetime(2026, 11, 5, tzinfo=UTC)
    with sessions.begin() as session:
        with pytest.raises(WorkloadIntentUnavailable):
            allocate_workload_intent(session, NODES)
        _, taken = take_workload_intent(session, {node: 3 for node in NODES})
        assert taken == (NODES[0],)


def test_a_fenced_spark_taken_by_a_newer_intent_is_counted_out_of_a_raise(sessions):
    """The recovery path raises a kept ordinal and must see a Spark that moved on."""
    with sessions.begin() as session:
        session.get(AgentNode, NODES[1]).workload_intent_ordinal = 6
    with sessions.begin() as session:
        assert raise_workload_intent(session, NODES, 5) == 1
    with sessions.begin() as session:
        assert raise_workload_intent(session, NODES, 6) == 2
    assert _ordinals(sessions) == {NODES[0]: 6, NODES[1]: 6}
