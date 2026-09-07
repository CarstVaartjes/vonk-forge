from __future__ import annotations

import hashlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker
from vonk_control.agent_jobs import AgentJobService
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    Base,
    Job,
)

from .runtime_identity_support import claim_agent

NOW = datetime(2026, 9, 7, tzinfo=UTC)
NODES = ("spk_" + "a" * 32, "spk_" + "b" * 32)
REVISION = "a" * 64
CAPABILITIES = ("agent.runtime.rust.v1", "recipe.stop")
PAYLOAD = {
    "schema_version": 1,
    "run_id": "00000000-0000-4000-8000-000000000001",
    "plan_digest": REVISION,
}


@pytest.fixture
def queue(postgres_engine):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        for index, node in enumerate(NODES):
            session.add(
                AgentNode(
                    node_id=node,
                    state="active",
                    protocol_version=3,
                    capabilities=list(CAPABILITIES),
                )
            )
        session.flush()
        for index, node in enumerate(NODES):
            session.add(
                AgentCertificate(
                    serial=f"serial-{index}",
                    node_id=node,
                    not_before=NOW - timedelta(seconds=1),
                    not_after=NOW + timedelta(hours=1),
                    fingerprint=f"fingerprint-{index}",
                )
            )
        parent = Job(
            request_id=str(uuid.uuid4()),
            kind="agent.operations",
            state="queued",
            actor="operator",
            authority_revision=REVISION,
            targets=list(NODES),
            payload_digest=hashlib.sha256(b"{}").hexdigest(),
            payload={},
            current_attempt=0,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(parent)
    services = [AgentJobService(sessions, clock=lambda: NOW) for _ in NODES]
    operations = [
        services[0].enqueue(parent.id, node, "recipe.stop", REVISION, PAYLOAD)
        for node in NODES
    ]
    return sessions, parent, services, operations


def _claim(service, index):
    return claim_agent(
        service,
        NODES[index],
        f"serial-{index}",
        30,
        protocol_version=3,
        capabilities=CAPABILITIES,
    )


def _concurrent_node_lock_calls(engine, calls):
    """Force both transactions to reach their first node lock together."""
    barrier = threading.Barrier(2)
    seen = set()
    guard = threading.Lock()

    def before(_conn, _cursor, statement, _parameters, context, _many):
        if "FROM agent_nodes" not in statement or "FOR UPDATE" not in statement:
            return
        identity = threading.get_ident()
        with guard:
            if identity in seen:
                return
            seen.add(identity)
        context.first_test_node_lock = True
        barrier.wait(timeout=5)

    def after(_conn, _cursor, _statement, _parameters, context, _many):
        if getattr(context, "first_test_node_lock", False):
            # With opposite individual locks this lets both holders advance;
            # a correctly ordered target-set lock serializes the transactions.
            time.sleep(0.1)

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call) for call in calls]
            return [future.result(timeout=15) for future in futures]
    finally:
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)


def test_dual_node_claims_lock_complete_scope_before_identity(queue, postgres_engine):
    _, _, services, operations = queue
    claims = _concurrent_node_lock_calls(
        postgres_engine,
        [lambda: _claim(services[0], 0), lambda: _claim(services[1], 1)],
    )
    assert all(claim is not None for claim in claims)
    assert {claim.operation_id for claim in claims} == {op.id for op in operations}
    assert len({claim.fence for claim in claims}) == 2


@pytest.mark.parametrize("action", ("heartbeat", "succeed"))
def test_dual_node_active_attempts_share_the_same_lock_order(
    queue, postgres_engine, action
):
    sessions, parent, services, _ = queue
    claims = [_claim(service, index) for index, service in enumerate(services)]
    assert all(claim is not None for claim in claims)

    def complete(index):
        if action == "heartbeat":
            return services[index].heartbeat(
                claims[index].fence, {"phase": "stopping"}, 30
            )
        return services[index].succeed(claims[index].fence, {"stopped": True})

    _concurrent_node_lock_calls(
        postgres_engine, [lambda: complete(0), lambda: complete(1)]
    )
    with sessions() as session:
        rows = list(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == parent.id)
            )
        )
        assert {row.state for row in rows} == (
            {"running"} if action == "heartbeat" else {"succeeded"}
        )
        if action == "succeed":
            assert session.get(Job, parent.id).state == "succeeded"


def _before_first_node_lock(engine, mutation):
    def before(_conn, _cursor, statement, _parameters, _context, _many):
        if (
            "FROM agent_nodes" in statement
            and "FOR UPDATE" in statement
            and not before.done
        ):
            before.done = True
            mutation()

    before.done = False
    event.listen(engine, "before_cursor_execute", before)
    return before


def test_changed_target_scope_is_rejected_after_unlocked_hint(queue, postgres_engine):
    sessions, parent, services, _ = queue

    def change_scope():
        with sessions.begin() as session:
            session.get(Job, parent.id).targets = [NODES[0]]

    hook = _before_first_node_lock(postgres_engine, change_scope)
    try:
        assert _claim(services[0], 0) is None
    finally:
        event.remove(postgres_engine, "before_cursor_execute", hook)


def test_changed_candidate_does_not_claim_another_operation(queue, postgres_engine):
    sessions, parent, services, operations = queue
    replacement = []

    def replace_candidate():
        with sessions.begin() as session:
            session.get(AgentOperation, operations[0].id).state = "cancelled"
        replacement.append(
            services[1].enqueue(parent.id, NODES[0], "recipe.stop", REVISION, PAYLOAD)
        )

    hook = _before_first_node_lock(postgres_engine, replace_candidate)
    try:
        assert _claim(services[0], 0) is None
    finally:
        event.remove(postgres_engine, "before_cursor_execute", hook)
    assert _claim(services[0], 0).operation_id == replacement[0].id


def test_revocation_between_hint_and_lock_stays_fail_closed(queue, postgres_engine):
    sessions, _, services, _ = queue

    def revoke():
        with sessions.begin() as session:
            session.get(AgentNode, NODES[0]).revoked_at = NOW

    hook = _before_first_node_lock(postgres_engine, revoke)
    try:
        assert _claim(services[0], 0) is None
    finally:
        event.remove(postgres_engine, "before_cursor_execute", hook)


def test_dual_node_enqueues_follow_claim_lock_order(queue, postgres_engine):
    _, parent, services, _ = queue
    operations = _concurrent_node_lock_calls(
        postgres_engine,
        [
            lambda: services[0].enqueue(
                parent.id, NODES[0], "recipe.stop", REVISION, PAYLOAD
            ),
            lambda: services[1].enqueue(
                parent.id, NODES[1], "recipe.stop", REVISION, PAYLOAD
            ),
        ],
    )
    assert {operation.node_id for operation in operations} == set(NODES)
