from __future__ import annotations

import hashlib
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control.agent_jobs import AgentJobService
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    Base,
    Job,
)
from vonk_control.run_admission import RunAdmissionBusy

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
                    workload_intent_ordinal=1,
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
    assert {claim.operation_id for claim in claims if claim is not None} == {
        op.id for op in operations
    }
    assert len({claim.fence for claim in claims if claim is not None}) == 2


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
    replaced = _claim(services[0], 0)
    assert replaced is not None
    assert replaced.operation_id == replacement[0].id


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
    sessions, parent, services, original = queue
    operation_ids = (str(uuid.uuid4()), str(uuid.uuid4()))
    expected_key = f"vonk-admission:node:{NODES[0]}"
    first_key_owner: int | None = None
    owner_guard = threading.Lock()
    owner_has_key = threading.Event()
    release_owner = threading.Event()
    observed_key_threads: set[int] = set()

    def hold_first_key_owner(
        _connection, _cursor, statement, parameters, _context, _many
    ):
        nonlocal first_key_owner
        if "pg_try_advisory_xact_lock" not in statement:
            return
        values = parameters if isinstance(parameters, dict) else {}
        if values.get("key") != expected_key:
            return
        thread_id = threading.get_ident()
        with owner_guard:
            observed_key_threads.add(thread_id)
            if first_key_owner is None:
                first_key_owner = thread_id
            owns_key = thread_id == first_key_owner
        if owns_key:
            owner_has_key.set()
            assert release_owner.wait(timeout=10), "test did not release the key owner"

    def enqueue(index):
        try:
            with sessions.begin() as session:
                return services[index].enqueue_in_session(
                    session,
                    parent.id,
                    NODES[index],
                    "recipe.stop",
                    REVISION,
                    PAYLOAD,
                    operation_id=operation_ids[index],
                )
        except RunAdmissionBusy as error:
            return error

    event.listen(postgres_engine, "after_cursor_execute", hold_first_key_owner)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(enqueue, index) for index in range(2)]
            try:
                assert owner_has_key.wait(timeout=10)
                completed, _ = wait(
                    futures, timeout=3, return_when=FIRST_COMPLETED
                )
                assert len(completed) == 1, "contending enqueue did not return promptly"
                refusal = next(iter(completed)).result()
                assert isinstance(refusal, RunAdmissionBusy), refusal
                assert len(observed_key_threads) == 2
                with sessions() as observer:
                    visible = tuple(
                        observer.scalars(
                            select(AgentOperation).where(
                                AgentOperation.parent_job_id == parent.id
                            )
                        )
                    )
                assert {operation.id for operation in visible} == {
                    operation.id for operation in original
                }
            finally:
                release_owner.set()
            results = [future.result(timeout=10) for future in futures]
    finally:
        release_owner.set()
        event.remove(postgres_engine, "after_cursor_execute", hold_first_key_owner)

    busy_indexes = [
        index
        for index, result in enumerate(results)
        if isinstance(result, RunAdmissionBusy)
    ]
    assert len(busy_indexes) == 1
    completed_index = 1 - busy_indexes[0]
    assert results[completed_index].id == operation_ids[completed_index]

    retried = enqueue(busy_indexes[0])
    assert not isinstance(retried, RunAdmissionBusy)
    assert retried.id == operation_ids[busy_indexes[0]]
    with sessions() as observer:
        final = tuple(
            observer.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == parent.id
                )
            )
        )
    new_rows = {operation.id: operation.node_id for operation in final}
    assert len(final) == len(original) + 2
    assert {
        operation_id: new_rows[operation_id]
        for operation_id in operation_ids
    } == dict(zip(operation_ids, NODES, strict=True))
