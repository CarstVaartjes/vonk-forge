"""Agent contact traffic never holds the node rows admission and publication lock.

Production 2026-10-25 (release 037bf432): profile loads waited on
``admission.capacity_busy`` and PostgreSQL reported ``DeadlockDetected`` on an
``agent_nodes`` row while no workload ran; every agent call held that row FOR
UPDATE. Contact facts now live on ``agent_node_contacts``, written by upsert.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    lock_admission_rows,
    set_admission_timeout,
)
from vonk_control.agent_contacts import record_observation_delivery
from vonk_control.agent_jobs import AgentJobService
from vonk_control.auth import AgentIdentity, AgentSource
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentNodeContact,
    Base,
    Job,
)

from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import claim_agent

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
NODES = ("spk_" + "a" * 32, "spk_" + "b" * 32)
REVISION = "a" * 64
_NODE_LOCK = ("FOR UPDATE", "FOR NO KEY UPDATE", "FOR SHARE", "FOR KEY SHARE")


def _source(index: int) -> AgentSource:
    return AgentSource(
        identity=AgentIdentity(
            node_id=NODES[index],
            certificate_serial=f"serial-{index}",
            certificate_fingerprint=f"fingerprint-{index}",
            verified=True,
        ),
        management_address=f"10.0.0.{11 + index}",
    )


@pytest.fixture
def fleet(postgres_engine):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node in NODES:
            session.add(
                AgentNode(
                    node_id=node,
                    state=NodeIdentityState.ACTIVE,
                    workload_intent_ordinal=1,
                )
            )
        session.flush()
        for index, node in enumerate(NODES):
            session.add(AgentNodeContact(node_id=node))
            session.add(
                AgentCertificate(
                    serial=f"serial-{index}",
                    node_id=node,
                    not_before=NOW - timedelta(seconds=1),
                    not_after=NOW + timedelta(hours=1),
                    fingerprint=f"fingerprint-{index}",
                )
            )
        payload = {"workload_intent_ordinal": 1}
        parent = Job(
            request_id=str(uuid.uuid4()),
            kind="agent.operations",
            state="queued",
            actor="operator",
            authority_revision=REVISION,
            targets=list(NODES),
            payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
            payload=payload,
            current_attempt=0,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(parent)
    service = AgentJobService(sessions, clock=lambda: NOW)
    for node in NODES:
        service.enqueue(
            parent.id,
            node,
            "recipe.stop",
            REVISION,
            recipe_stop_payload(node, plan_digest=REVISION),
        )
    claims = [
        claim_agent(service, node, f"serial-{index}", source=_source(index))
        for index, node in enumerate(NODES)
    ]
    assert all(claim is not None for claim in claims)
    return sessions, service, parent, claims


def _contact_calls(sessions, service, claims, index: int):
    """Every contact writer one Spark exercises on each pass."""
    claim = claims[index]
    return (
        lambda: service.heartbeat(
            claim.fence, {"phase": "stopping"}, 30, source=_source(index)
        ),
        lambda: claim_agent(
            service,
            NODES[index],
            f"serial-{index}",
            source=_source(index),
            claim_fence=claim.fence,
        ),
        lambda: _deliver_observation(sessions, NODES[index]),
    )


def _deliver_observation(sessions, node_id: str) -> None:
    with sessions.begin() as session:
        record_observation_delivery(session, node_id, 204, NOW)


def _admit(sessions) -> None:
    """Capacity admission's row lock on the target nodes (NOWAIT)."""
    with sessions.begin() as session:
        set_admission_timeout(session)
        lock_admission_rows(
            session,
            (
                AdmissionRowLock(
                    "target-agent-nodes",
                    AgentNode,
                    select(AgentNode).where(AgentNode.node_id.in_(NODES)),
                ),
            ),
        )


def _publish(sessions) -> None:
    """RunSwitch image publication's NOWAIT lock on its target nodes."""
    with sessions.begin() as session:
        list(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(NODES))
                .order_by(AgentNode.node_id)
                .with_for_update(nowait=True)
            )
        )


def _job_then_nodes(sessions, job_id: str) -> None:
    """The RunSwitch loop's order: its Job row first, then the target nodes."""
    with sessions.begin() as session:
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.get(Job, job_id, with_for_update=True)
        list(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(NODES))
                .order_by(AgentNode.node_id)
                .with_for_update()
            )
        )


def test_open_contact_transaction_leaves_admission_and_publication_free(
    fleet, postgres_engine
):
    """Fails on main: a heartbeat held every target node FOR UPDATE until commit."""
    sessions, service, _, claims = fleet
    engine = postgres_engine
    paused, release = threading.Event(), threading.Event()
    contact_thread: list[int] = []

    def hold_before_commit(_connection) -> None:
        if threading.get_ident() in contact_thread:
            paused.set()
            release.wait(timeout=30)

    event.listen(engine, "commit", hold_before_commit)
    errors: list[BaseException] = []

    def contact() -> None:
        contact_thread.append(threading.get_ident())
        try:
            service.heartbeat(
                claims[0].fence, {"phase": "stopping"}, 30, source=_source(0)
            )
        except Exception as error:  # noqa: BLE001 - reported to the test thread
            errors.append(error)

    worker = threading.Thread(target=contact)
    worker.start()
    try:
        assert paused.wait(timeout=15), "contact transaction never reached commit"
        # The contact transaction holds everything it took; admission and
        # publication must still take the node rows without waiting.
        _admit(sessions)
        _publish(sessions)
    finally:
        release.set()
        worker.join(timeout=30)
        event.remove(engine, "commit", hold_before_commit)
    assert not errors
    with sessions() as session:
        contact = session.get(AgentNodeContact, NODES[0])
        assert contact is not None
        assert contact.last_seen_at is not None
        assert contact.management_address == "10.0.0.11"


def test_hammered_contacts_never_refuse_admission_or_deadlock(fleet, postgres_engine):
    """Agents hammer contact writes while admission, publication and the
    RunSwitch Job-then-nodes order run: no busy refusal, no deadlock, and no
    contact statement locks or updates agent_nodes."""
    sessions, service, parent, claims = fleet
    engine = postgres_engine
    agents: set[int] = set()
    node_statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if threading.get_ident() not in agents:
            return
        lowered = " ".join(statement.split())
        if (
            "UPDATE agent_nodes" in lowered
            or "FROM agent_nodes" in lowered
            and any(mode in lowered for mode in _NODE_LOCK)
        ):
            node_statements.append(lowered)

    event.listen(engine, "before_cursor_execute", record)
    stop = threading.Event()
    failures: list[BaseException] = []
    contacts = [0]

    def agent(index: int) -> None:
        agents.add(threading.get_ident())
        calls = _contact_calls(sessions, service, claims, index)
        while not stop.is_set():
            for call in calls:
                try:
                    call()
                except Exception as error:  # noqa: BLE001 - reported to the test thread
                    failures.append(error)
                    return
                contacts[0] += 1

    threads = [
        threading.Thread(target=agent, args=(index,))
        for index in (0, 0, 1, 1)  # two connections per Spark
    ]
    for thread in threads:
        thread.start()
    refusals: list[BaseException] = []
    rounds = 0
    deadline = time.monotonic() + 3  # wall-clock: hammer duration, not a limit
    try:
        while time.monotonic() < deadline and not failures:
            for step in (
                lambda: _admit(sessions),
                lambda: _publish(sessions),
                lambda: _job_then_nodes(sessions, parent.id),
            ):
                try:
                    step()
                except (AdmissionLockBusy, DBAPIError) as error:
                    refusals.append(error)
            rounds += 1
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=30)
        event.remove(engine, "before_cursor_execute", record)

    assert not failures, failures[:3]
    assert not refusals, refusals[:3]
    assert node_statements == []
    assert rounds > 0 and contacts[0] > 0
    with sessions() as session:
        stored = session.scalars(select(AgentNodeContact)).all()
        assert {row.node_id: row.recipe_observation_http_status for row in stored} == {
            NODES[0]: 204,
            NODES[1]: 204,
        }
        assert all(row.last_seen_at is not None for row in stored)
