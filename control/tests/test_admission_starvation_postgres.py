"""A waiting admission eventually wins, and every refusal says why."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from vonk_control import telemetry_maintenance
from vonk_control.admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    acquire_admission_keys,
    lock_admission_rows,
    node_admission_key,
    patient_admission,
)
from vonk_control.models import AgentNode, Base, NodeTelemetrySample

NODE = "spk_" + "a" * 32
NOW = datetime(2026, 10, 5, tzinfo=UTC)


@pytest.fixture
def sessions(postgres_engine):
    Base.metadata.create_all(postgres_engine)
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE,
                state="active",
                protocol_version=3,
                workload_intent_ordinal=1,
            )
        )
    return factory


def _tick_forever(sessions, stop: threading.Event, started: threading.Event) -> None:
    """A background writer that takes the node key again as soon as it lets go."""

    deadline = time.monotonic() + 8
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            with sessions.begin() as session:
                acquire_admission_keys(
                    session, (node_admission_key(NODE),), holder="order-reconcile"
                )
                started.set()
                stop.wait(0.02)
        except AdmissionLockBusy:
            stop.wait(0.02)  # backs off a node that is being served


def test_a_patient_admission_wins_against_a_constantly_ticking_writer(
    sessions,
) -> None:
    stop = threading.Event()
    started = threading.Event()
    ticker = threading.Thread(target=_tick_forever, args=(sessions, stop, started))
    ticker.start()
    try:
        assert started.wait(timeout=5), "ticker did not acquire admission"
        with patient_admission(3), sessions.begin() as session:
            acquire_admission_keys(
                session, (node_admission_key(NODE),), holder="run-admission"
            )
            lock_admission_rows(
                session,
                (
                    AdmissionRowLock(
                        "nodes",
                        AgentNode,
                        select(AgentNode).where(AgentNode.node_id == NODE),
                    ),
                ),
            )
    finally:
        stop.set()
        ticker.join(timeout=5)
        assert not ticker.is_alive(), "ticker outlived its test"
    with sessions.begin() as session:
        acquire_admission_keys(
            session, (node_admission_key(NODE),), holder="run-admission"
        )


def test_contended_admission_returns_and_a_fresh_owner_acquires(sessions) -> None:
    with sessions.begin() as holder:
        acquire_admission_keys(
            holder, (node_admission_key(NODE),), holder="order-reconcile"
        )
        started = time.monotonic()
        with pytest.raises(Exception), sessions.begin() as taker:  # noqa: B017 -- effects and subsequent admission witness rejection
            acquire_admission_keys(taker, (node_admission_key(NODE),))
        assert time.monotonic() - started < 3
    with sessions.begin() as fresh:
        acquire_admission_keys(
            fresh, (node_admission_key(NODE),), holder="fresh-admission"
        )


def test_contended_row_lock_ends_and_next_transaction_acquires(sessions) -> None:
    statement = AdmissionRowLock(
        "nodes", AgentNode, select(AgentNode).where(AgentNode.node_id == NODE)
    )
    with sessions.begin() as holder:
        holder.scalars(select(AgentNode).with_for_update()).all()
        started = time.monotonic()
        with pytest.raises(Exception), sessions.begin() as taker:  # noqa: B017 -- effects and subsequent admission witness rejection
            lock_admission_rows(taker, (statement,))
        assert time.monotonic() - started < 3
    with sessions.begin() as fresh:
        lock_admission_rows(fresh, (statement,))


def test_admission_rollback_releases_keys_for_the_next_request(sessions) -> None:
    with pytest.raises(RuntimeError), sessions.begin() as failed:
        acquire_admission_keys(failed, (node_admission_key(NODE),))
        raise RuntimeError("injected failure after admission")
    with sessions.begin() as fresh:
        acquire_admission_keys(fresh, (node_admission_key(NODE),))


def test_telemetry_maintenance_skips_a_node_an_admission_holds(sessions) -> None:
    boot = "11111111-1111-4111-8111-111111111111"
    with sessions.begin() as session:
        session.add(
            NodeTelemetrySample(
                id="sample-old",
                node_id=NODE,
                boot_id=boot,
                observed_at=NOW - timedelta(days=3),
                received_at=NOW - timedelta(days=3),
            )
        )
    maintenance = telemetry_maintenance.TelemetryMaintenance(
        sessions, clock=lambda: NOW
    )
    with sessions.begin() as holder:
        holder.scalars(select(AgentNode).with_for_update()).all()
        started = time.monotonic()
        maintenance.run_once()  # must neither wait for the node nor fail
        assert time.monotonic() - started < 3
    with sessions.begin() as session:
        assert session.get(NodeTelemetrySample, "sample-old") is not None
    maintenance.run_once()
    with sessions.begin() as session:
        assert session.get(NodeTelemetrySample, "sample-old") is None
