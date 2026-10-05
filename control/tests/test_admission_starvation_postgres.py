"""A waiting admission eventually wins, and every refusal says why."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker
from vonk_control import telemetry_maintenance
from vonk_control.admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    acquire_admission_keys,
    busy_detail,
    lock_admission_rows,
    node_admission_key,
    patient_admission,
    report_admission_locks,
)
from vonk_control.models import AgentNode, Base, NodeTelemetrySample
from vonk_control.run_admission import RunAdmissionBusy

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


def _tick_forever(sessions, stop: threading.Event) -> None:
    """A background writer that takes the node key again as soon as it lets go."""

    while not stop.is_set():
        try:
            with sessions.begin() as session:
                acquire_admission_keys(
                    session, (node_admission_key(NODE),), holder="order-reconcile"
                )
                time.sleep(0.02)
        except AdmissionLockBusy:
            time.sleep(0.02)  # backs off a node that is being served


def test_a_patient_admission_wins_against_a_constantly_ticking_writer(
    sessions,
) -> None:
    stop = threading.Event()
    ticker = threading.Thread(target=_tick_forever, args=(sessions, stop))
    ticker.start()
    try:
        time.sleep(0.1)
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


def test_an_impatient_admission_is_refused_and_names_the_holder(sessions) -> None:
    with sessions.begin() as holder:
        acquire_admission_keys(
            holder, (node_admission_key(NODE),), holder="order-reconcile"
        )
        with sessions.begin() as taker, pytest.raises(AdmissionLockBusy) as busy:
            acquire_admission_keys(taker, (node_admission_key(NODE),))
        report = report_admission_locks(taker)
    assert busy.value.holder == "order-reconcile"
    assert any(
        item.node_id == NODE and item.holder == "order-reconcile"
        for item in report.held
    )


def test_a_refused_row_lock_names_its_sqlstate_and_the_open_transactions(
    sessions,
) -> None:
    statement = AdmissionRowLock(
        "nodes", AgentNode, select(AgentNode).where(AgentNode.node_id == NODE)
    )
    with sessions.begin() as holder:
        holder.execute(
            text("SELECT set_config('application_name', 'vonk:probe', true)")
        )
        holder.scalars(select(AgentNode).with_for_update()).all()
        with sessions.begin() as taker, pytest.raises(AdmissionLockBusy) as busy:
            lock_admission_rows(taker, (statement,))
    assert busy.value.sqlstate == "55P03"
    assert "55P03" in str(busy.value)
    assert "probe" in (busy.value.activity or "")
    chained = RunAdmissionBusy("run capacity writer is busy")
    chained.__cause__ = busy.value
    assert "55P03" in busy_detail(chained)
    assert "capacity writer" in busy_detail(chained)


def test_a_wait_that_is_not_lock_contention_names_its_own_cause() -> None:
    detail = busy_detail(RunAdmissionBusy("run mapping is waiting to become ready"))
    assert "run mapping is waiting to become ready" in detail
    assert "capacity writer" not in detail


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
