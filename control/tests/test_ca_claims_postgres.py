"""Real PostgreSQL proves claims do not hold unrelated node progress."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from vonk_control.db.connections import build_engine
from vonk_control.db.connections import build_engine as db_build_engine
from vonk_control.enrollment.contract import EnrollmentGrant
from vonk_control.enrollment.service import EnrollmentService
from vonk_control.model_primitives import Base
from vonk_control.models.fleet import (
    AgentCertificate,
    AgentEnrollment,
    AgentEnrollmentGrant,
)
from vonk_control.pki import IssuedCertificate
from vonk_control.settings import DATABASE_WAIT_BUDGETS

from .ca_material import NODE_ID, NOW
from .test_enrollment import OTHER_NODE_ID, RecordingAuthority, csr, evidence


class SlowAuthority(RecordingAuthority):
    """Holds one node's issuance open until released."""

    def __init__(self) -> None:
        super().__init__()
        self.pause_node: str | None = None
        self.entered = Event()
        self.release = Event()

    def issue_node(self, node_id, public_key_pem, now, *, request):
        if node_id == self.pause_node:
            self.entered.set()
            assert self.release.wait(30)
        return super().issue_node(node_id, public_key_pem, now, request=request)


def prepare(postgres_engine, provider):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    return sessions, EnrollmentService(sessions, provider, clock=lambda: NOW)


def enroll(service, node_id):
    request = csr(node_id)
    grant = service.create(node_id, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    issued = service.submit(grant.token, request, evidence(request, node_id=node_id))
    assert isinstance(issued, IssuedCertificate)
    return issued


@pytest.mark.parametrize("purpose", ["enrollment", "rotation"])
def test_slow_issuer_does_not_hold_an_unrelated_postgres_claim(
    postgres_engine, purpose
):
    """Catches a process lock held while one provider request waits externally."""
    provider = SlowAuthority()
    entered, release = provider.entered, provider.release
    sessions, service = prepare(postgres_engine, provider)
    sources = (
        {node: enroll(service, node) for node in (NODE_ID, OTHER_NODE_ID)}
        if purpose == "rotation"
        else {}
    )
    requests = {node: csr(node) for node in (NODE_ID, OTHER_NODE_ID)}
    grants = (
        {node: service.create(node, "admin", 600) for node in requests}
        if purpose == "enrollment"
        else {}
    )

    def issue(node):
        if purpose == "rotation":
            return service.renew(node, sources[node].serial, requests[node])
        grant = grants[node]
        assert isinstance(grant, EnrollmentGrant)
        return service.submit(
            grant.token,
            requests[node],
            evidence(requests[node], node_id=node),
        )

    provider.pause_node = NODE_ID
    with ThreadPoolExecutor(max_workers=2) as pool:
        blocked = pool.submit(issue, NODE_ID)
        assert entered.wait(30)
        with postgres_engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'idle in transaction'"
                    )
                )
                == 0
            )
        try:
            independent = pool.submit(issue, OTHER_NODE_ID).result(timeout=30)
            assert isinstance(independent, IssuedCertificate)
            assert independent.node_id == OTHER_NODE_ID
            assert not blocked.done()
            with sessions() as session:
                assert session.get(AgentCertificate, independent.serial) is not None
        finally:
            release.set()
        issued = blocked.result(30)
        assert isinstance(issued, IssuedCertificate)
        assert issued.node_id == NODE_ID


def test_postgres_grant_lock_conflict_ends_observation_and_same_request_recovers(
    postgres_engine,
):
    """Catches SQL contention escaping before effects or consuming a valid grant."""
    provider = RecordingAuthority()
    engine = build_engine(
        postgres_engine.url.render_as_string(hide_password=False),
        component="test-enrollment",
    )
    try:
        sessions, enrollment = prepare(engine, provider)
        grant = enrollment.create(NODE_ID, "admin", 600)
        assert isinstance(grant, EnrollmentGrant)
        request = csr(NODE_ID)
        with sessions.begin() as blocker:
            blocker.get(AgentEnrollmentGrant, grant.id, with_for_update=True)
            started = time.monotonic()
            enrollment.submit(grant.token, request, evidence(request))
            budget = 8 * DATABASE_WAIT_BUDGETS.admission_lock_timeout_ms / 1000 + 5
            assert time.monotonic() - started <= budget
            assert not provider.calls
            with sessions() as observer:
                original = observer.get(AgentEnrollmentGrant, grant.id)
                assert original is not None
                assert original.consumed_at is None
                assert observer.scalar(select(AgentEnrollment)) is None
        restarted = EnrollmentService(sessions, provider, clock=lambda: NOW)
        issued = restarted.submit(grant.token, request, evidence(request))
        assert isinstance(issued, IssuedCertificate)
        assert restarted.submit(grant.token, request, evidence(request)) == issued
        assert len(provider.calls) == 1
    finally:
        engine.dispose()


def test_enrollment_transactions_commit_durably_on_async_controller_connections(
    postgres_engine,
):

    engine = db_build_engine(postgres_engine.url.render_as_string(hide_password=False))
    try:
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SHOW synchronous_commit").scalar() == "off"
            )
        sessions = sessionmaker(engine, expire_on_commit=False)
        service = EnrollmentService(sessions, RecordingAuthority(), clock=lambda: NOW)
        with service._transaction() as session:
            assert session.execute(text("SHOW synchronous_commit")).scalar() == "on"
    finally:
        engine.dispose()
