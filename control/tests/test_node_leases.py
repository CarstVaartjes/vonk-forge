from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.models import AgentNode, Base, NodeMutationLease
from vonk_control.node_leases import NodeLeaseConflict, NodeLeaseService

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
NOW = datetime(2026, 8, 5, tzinfo=UTC)


def _sessions(tmp_path) -> sessionmaker[Session]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'node-leases.sqlite'}",
        connect_args={"timeout": 10},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add_all(
            AgentNode(node_id=node_id, state="active", capabilities=[])
            for node_id in (NODE_A, NODE_B)
        )
    return sessions


def test_group_acquisition_is_durable_ordered_and_restart_idempotent(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    service = NodeLeaseService(clock=lambda: NOW)
    owner_id = str(uuid.uuid4())

    with sessions.begin() as session:
        grant = service.acquire_in_session(
            session,
            (NODE_B, NODE_A),
            owner_kind="update-rollout",
            owner_id=owner_id,
        )

    assert grant.node_ids == (NODE_A, NODE_B)
    with sessions.begin() as session:
        restarted = service.acquire_in_session(
            session,
            (NODE_A, NODE_B),
            owner_kind="update-rollout",
            owner_id=owner_id,
        )
        rows = list(
            session.scalars(
                select(NodeMutationLease).order_by(NodeMutationLease.node_id)
            )
        )

    assert restarted == grant
    assert [row.node_id for row in rows] == [NODE_A, NODE_B]
    assert {row.fence for row in rows} == {grant.fence}
    assert {row.state for row in rows} == {"held"}
    assert all(row.acquired_at.replace(tzinfo=UTC) == NOW for row in rows)


def test_group_conflict_does_not_leave_a_partial_lease(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    service = NodeLeaseService(clock=lambda: NOW)
    with sessions.begin() as session:
        service.acquire_in_session(
            session,
            (NODE_A,),
            owner_kind="reconciliation",
            owner_id=str(uuid.uuid4()),
        )

    with sessions.begin() as session:
        with pytest.raises(NodeLeaseConflict) as caught:
            service.acquire_in_session(
                session,
                (NODE_A, NODE_B),
                owner_kind="update-rollout",
                owner_id=str(uuid.uuid4()),
            )
        assert caught.value.node_ids == (NODE_A,)

    with sessions() as session:
        rows = list(session.scalars(select(NodeMutationLease)))
    assert [row.node_id for row in rows] == [NODE_A]


def test_release_requires_exact_fence_and_two_phase_release(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    service = NodeLeaseService(clock=lambda: NOW)
    with sessions.begin() as session:
        grant = service.acquire_in_session(
            session,
            (NODE_A,),
            owner_kind="update-rollout",
            owner_id=str(uuid.uuid4()),
        )

    stale = replace(grant, fence=str(uuid.uuid4()))
    with sessions.begin() as session, pytest.raises(NodeLeaseConflict):
        service.mark_releasing_in_session(session, stale)

    later = NOW + timedelta(minutes=1)
    service = NodeLeaseService(clock=lambda: later)
    with sessions.begin() as session:
        service.mark_releasing_in_session(session, grant)
    with sessions() as session:
        row = session.get(NodeMutationLease, NODE_A)
        assert row is not None
        assert row.state == "releasing"
        assert row.updated_at.replace(tzinfo=UTC) == later

    with sessions.begin() as session:
        recovered = service.owned_grant_in_session(
            session,
            (NODE_A,),
            owner_kind=grant.owner_kind,
            owner_id=grant.owner_id,
        )
    assert recovered is not None
    assert recovered.fence == grant.fence
    assert recovered.state == "releasing"

    with sessions.begin() as session:
        service.release_in_session(session, recovered)
    with sessions() as session:
        assert session.get(NodeMutationLease, NODE_A) is None


def test_sqlite_concurrent_acquire_has_exactly_one_owner(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    barrier = threading.Barrier(2)

    def acquire(owner_id: str) -> str | None:
        service = NodeLeaseService(clock=lambda: NOW)
        barrier.wait(timeout=5)
        try:
            with sessions.begin() as session:
                service.acquire_in_session(
                    session,
                    (NODE_A,),
                    owner_kind="update-rollout",
                    owner_id=owner_id,
                )
            return owner_id
        except NodeLeaseConflict:
            return None

    owners = (str(uuid.uuid4()), str(uuid.uuid4()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(acquire, owners))

    winner = next(result for result in results if result is not None)
    assert sum(result is not None for result in results) == 1
    with sessions() as session:
        row = session.get(NodeMutationLease, NODE_A)
        assert row is not None
        assert row.owner_id == winner
