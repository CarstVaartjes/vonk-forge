from __future__ import annotations

import hashlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentResult, canonical_message
from vonk_control.agent_jobs import AgentJobService, StaleAgentAttempt
from vonk_control.auth import TokenCodec
from vonk_control.enrollment import EnrollmentService
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
)
from vonk_control.operation_api import durable_operation_services
from vonk_control.pki import CertificateAuthority, IssuedCertificate
from vonk_control.run_admission import RunAdmissionBusy

from .runtime_identity_support import claim_agent
from .test_agent_jobs import exercise_upgrade_reconnect

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
COMMIT = "a" * 64
STOP_PAYLOAD = {
    "schema_version": 1,
    "run_id": "00000000-0000-4000-8000-000000000001",
    "plan_digest": COMMIT,
}
STOP_RESULT = {"stopped": True}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class RevokingAuthority(CertificateAuthority):
    def issue_node(
        self, node_id: str, csr_pem: bytes, now: datetime
    ) -> IssuedCertificate:
        raise NotImplementedError

    def renew_node(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        request_id: str,
    ) -> IssuedCertificate:
        raise NotImplementedError

    def revocation_bundle(self, now: datetime) -> bytes:
        return b""

    def revoke_node(self, serial: str, now: datetime) -> None:
        return None


@pytest.fixture
def service(postgres_engine):
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    clock = Clock()
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    capabilities=[],
                    workload_intent_ordinal=1,
                )
            )
        session.flush()
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    not_before=clock.now - timedelta(seconds=1),
                    not_after=clock.now + timedelta(hours=1),
                    fingerprint=f"fingerprint-{serial}",
                )
            )
    return sessions, clock


def parent(sessions, clock) -> Job:
    payload = {"workload_intent_ordinal": 1}
    job = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_A, NODE_B],
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(job)
    return job


def state(sessions, job_id: str) -> str:
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job.state


def test_postgres_resume_transition_has_one_concurrent_winner(
    service, tmp_path
) -> None:
    sessions, clock = service
    job = parent(sessions, clock)
    jobs = AgentJobService(sessions, clock=clock)
    operation = jobs.enqueue(job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    original = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert original is not None
    jobs.wait_for_operator(original.fence, "operator must inspect the stopped effect")
    first = durable_operation_services(
        sessions,
        tmp_path / "routes-a",
        clock=clock,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )
    second = durable_operation_services(
        sessions,
        tmp_path / "routes-b",
        clock=clock,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )
    barrier = threading.Barrier(2)

    def resume(services) -> str:
        barrier.wait()
        try:
            services.resume_job(job.id)
            return "won"
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(resume, (first, second)))

    assert outcomes.count("won") == 1
    assert outcomes.count("conflict") == 1
    assert state(sessions, job.id) == "queued"
    resumed = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
        30,
        protocol_version=3,
        capabilities=(
            "agent.runtime.rust.v1",
            "recipe.stop",
            "agent.lifecycle.resume.exact.v1",
        ),
    )
    assert resumed is not None
    assert resumed.operation_id == operation.id
    assert resumed.attempt == original.attempt + 1


def test_postgres_claim_locks_only_operations_without_nullable_join(
    service, postgres_engine
) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if "FROM agent_operations" in statement and "FOR UPDATE" in statement:
            statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", record)
    try:
        assert claim_agent(jobs, NODE_A, "serial-a", 30) is not None
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)

    assert statements
    assert all("LEFT OUTER JOIN" not in statement for statement in statements)
    assert any(
        "FOR UPDATE OF agent_operations SKIP LOCKED" in statement
        for statement in statements
    )


@pytest.mark.parametrize("older_work", ("unadvertised", "exact-retry", "running"))
def test_postgres_upgrade_bypasses_unsupported_work_then_resumes_it(
    service, older_work
) -> None:
    sessions, clock = service
    exercise_upgrade_reconnect(
        (AgentJobService(sessions, clock=clock), sessions, clock), older_work
    )


@pytest.mark.parametrize(
    ("kind", "payload", "error_code", "auto_retry"),
    (
        (
            "artifact.distribution.v1",
            {"schema_version": 1, "authority_revision": COMMIT, "plan_digest": COMMIT},
            "agent_restart_interrupted",
            True,
        ),
        (
            "runtime.preflight.v1",
            {
                "schema_version": 1,
                "architecture": "linux-arm64",
                "source_build": False,
                "minimum_free_bytes": 0,
                "fabric_connectivity": "none",
                "fabric_minimum_mbps": 0,
                "mandatory_capabilities": [],
            },
            "agent_restart_interrupted",
            True,
        ),
        ("recipe.stop", STOP_PAYLOAD, "agent_restart_interrupted", True),
        (
            "artifact.distribution.v1",
            {"schema_version": 1, "authority_revision": COMMIT, "plan_digest": COMMIT},
            "operation_outcome_uncertain",
            False,
        ),
    ),
)
def test_postgres_restart_receipt_retries_only_exact_safe_operation(
    service, kind: str, payload: dict[str, object], error_code: str, auto_retry: bool
) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, kind, COMMIT, payload)
    capabilities = ["agent.runtime.rust.v1", kind]
    resume_capabilities = (
        [*capabilities, "agent.lifecycle.resume.exact.v1"]
        if kind == "recipe.stop"
        else capabilities
    )
    first = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
        30,
        protocol_version=3,
        capabilities=capabilities,
    )
    assert first is not None

    def restart_receipt(claim) -> AgentResult:
        return AgentResult.model_validate_json(
            canonical_message(
                {
                    **{
                        key: getattr(claim, key)
                        for key in (
                            "schema_version",
                            "job_id",
                            "operation_id",
                            "attempt",
                            "fence",
                            "node_id",
                            "deadline",
                        )
                    },
                    "state": "waiting-for-operator",
                    "result": {
                        "error_code": error_code,
                        "failure_kind": "uncertain-effect",
                        "uncertain": True,
                        "reason": "agent process restarted",
                    },
                }
            )
        )

    jobs.record_result(restart_receipt(first))
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        parent_row = session.get(Job, parent_job.id)
        assert stored is not None and parent_row is not None
        assert stored.state == "waiting-for-operator"
        due = stored.retry_due_at
        if not auto_retry:
            assert due is None
            assert parent_row.state == "waiting-for-operator"
        else:
            assert due is not None
            assert parent_row.state == "queued"
            assert stored.status_reason is not None
            assert "agent.lifecycle.resume.exact.v1" not in stored.status_reason
    jobs = AgentJobService(sessions, clock=clock)
    assert (
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            30,
            protocol_version=3,
            capabilities=resume_capabilities,
        )
        is None
    )
    if due is not None:
        clock.now = due.replace(tzinfo=UTC)
        if kind == "recipe.stop":
            assert (
                claim_agent(
                    jobs,
                    NODE_A,
                    "serial-a",
                    30,
                    protocol_version=3,
                    capabilities=capabilities,
                )
                is None
            )
            with sessions() as session:
                stored = session.get(AgentOperation, operation.id)
                assert stored is not None
                assert stored.status_reason is not None
                assert (
                    "Spark agent update required before exact recovery"
                    in stored.status_reason
                )
                assert stored.retry_due_at == due
        second = claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            30,
            protocol_version=3,
            capabilities=resume_capabilities,
        )
        assert second is not None
        assert second.operation_id == operation.id
        assert second.attempt == first.attempt + 1
        assert second.payload == first.payload
        for attempt_number in range(2, 7):
            jobs.record_result(restart_receipt(second))
            with sessions() as session:
                stored = session.get(AgentOperation, operation.id)
                parent_row = session.get(Job, parent_job.id)
                assert stored is not None and parent_row is not None
                assert stored.current_attempt == attempt_number
                assert stored.retry_due_at is not None
                assert (
                    clock.now < stored.retry_due_at <= clock.now + timedelta(seconds=60)
                )
                assert parent_row.state == "queued"
                due = stored.retry_due_at
            clock.now = due.replace(tzinfo=UTC)
            second = claim_agent(
                jobs,
                NODE_A,
                "serial-a",
                30,
                protocol_version=3,
                capabilities=resume_capabilities,
            )
            assert second is not None and second.attempt == attempt_number + 1
            assert (
                second.operation_id == operation.id and second.payload == first.payload
            )


def test_postgres_separate_services_cannot_claim_the_same_operation(service) -> None:
    sessions, clock = service
    first_service = AgentJobService(sessions, clock=clock)
    second_service = AgentJobService(sessions, clock=clock)
    operation = first_service.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    barrier = threading.Barrier(2)

    def claim(service):
        barrier.wait()
        return claim_agent(service, NODE_A, "serial-a", 30)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, (first_service, second_service)))

    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    assert claimed[0].operation_id == operation.id


@pytest.mark.parametrize("agent_action", ("claim", "heartbeat", "result"))
def test_postgres_revocation_serializes_agent_work_and_contact(
    service, postgres_engine, agent_action: str
) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    enrollment = EnrollmentService(sessions, RevokingAuthority(), clock=clock)
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = None
    original_deadline = None
    if agent_action != "claim":
        claim = claim_agent(jobs, NODE_A, "serial-a", 30, protocol_version=3)
        assert claim is not None
        original_deadline = claim.deadline
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.last_seen_at = None

    revocation_locked = threading.Event()
    release_revocation = threading.Event()
    revocation_errors: list[Exception] = []
    action_results: list[object] = []

    def pause_after_node_lock(
        _conn, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            threading.current_thread().name == "revoker"
            and "FROM agent_nodes" in statement
            and "FOR UPDATE OF agent_nodes" in statement
        ):
            revocation_locked.set()
            assert release_revocation.wait(timeout=5)

    def revoke() -> None:
        try:
            enrollment.revoke_node(NODE_A, "admin")
        except (
            AssertionError,
            OSError,
            RuntimeError,
            ValueError,
            SQLAlchemyError,
        ) as error:
            revocation_errors.append(error)

    def act() -> None:
        try:
            if agent_action == "claim":
                action_results.append(claim_agent(jobs, NODE_A, "serial-a", 30))
            elif agent_action == "heartbeat":
                assert claim is not None
                action_results.append(jobs.heartbeat(claim, {"phase": "checking"}, 60))
            else:
                assert claim is not None
                jobs.succeed(claim, STOP_RESULT)
                action_results.append(None)
        except (
            AssertionError,
            OSError,
            RuntimeError,
            ValueError,
            SQLAlchemyError,
        ) as error:
            action_results.append(error)

    event.listen(postgres_engine, "after_cursor_execute", pause_after_node_lock)
    try:
        revoker = threading.Thread(target=revoke, name="revoker")
        worker = threading.Thread(target=act, name="agent-worker")
        revoker.start()
        assert revocation_locked.wait(timeout=5)
        worker.start()
        time.sleep(0.25)
        assert worker.is_alive(), (
            "agent work must wait for the revocation identity lock"
        )
        release_revocation.set()
        revoker.join(timeout=5)
        worker.join(timeout=5)
    finally:
        release_revocation.set()
        event.remove(postgres_engine, "after_cursor_execute", pause_after_node_lock)

    assert not revoker.is_alive() and not worker.is_alive()
    assert not revocation_errors
    if agent_action == "claim":
        assert action_results == [None]
    else:
        assert len(action_results) == 1
        assert isinstance(action_results[0], StaleAgentAttempt)
    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        certificate = session.get(AgentCertificate, "serial-a")
        stored_operation = session.get(AgentOperation, operation.id)
        assert (
            node is not None and node.state == "retired" and node.last_seen_at is None
        )
        assert certificate is not None and certificate.state == "revoked"
        assert stored_operation is not None
        if agent_action == "claim":
            assert stored_operation.state == "queued"
            assert stored_operation.current_attempt == 0
        else:
            assert claim is not None
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == claim.attempt,
                )
            )
            assert attempt is not None and attempt.state == "running"
            assert attempt.lease_deadline.astimezone(UTC) == original_deadline
            assert attempt.progress is None and attempt.result is None


def test_postgres_expired_mutating_operation_schedules_bounded_exact_retry(
    service,
) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    first = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert first is not None

    clock.advance(seconds=30)
    assert claim_agent(jobs, NODE_A, "serial-a", 30) is None

    with sessions() as session:
        gated = session.get(AgentOperation, operation.id)
        assert gated is not None
        assert gated.state == "waiting-for-operator"
        assert gated.retry_disposition == "retry"
        assert gated.retry_disposition_attempt == 1
        assert gated.retry_due_at is not None
        due = gated.retry_due_at
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == 1,
            )
        )
        assert attempt is not None and attempt.state == "expired"
        assert session.get(Job, parent_job.id).state == "queued"  # type: ignore[union-attr]

    clock.now = due.replace(tzinfo=UTC)
    assert claim_agent(jobs, NODE_A, "serial-a", 30) is None
    second = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
        30,
        protocol_version=3,
        capabilities=[
            "agent.runtime.rust.v1",
            "recipe.stop",
            "agent.lifecycle.resume.exact.v1",
        ],
    )
    assert second is not None
    assert second.operation_id == first.operation_id
    assert second.attempt == 2

    clock.advance(seconds=30)
    assert claim_agent(jobs, NODE_A, "serial-a", 30) is None


@pytest.mark.parametrize("exhausted", (False, True))
def test_postgres_new_stop_supersedes_parked_old_retry_without_starvation(
    service, exhausted: bool
) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    old_parent = parent(sessions, clock)
    old = jobs.enqueue(old_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    first = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert first is not None
    jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    **{
                        key: getattr(first, key)
                        for key in (
                            "schema_version",
                            "job_id",
                            "operation_id",
                            "attempt",
                            "fence",
                            "node_id",
                            "deadline",
                        )
                    },
                    "state": "waiting-for-operator",
                    "result": {
                        "error_code": "agent_restart_interrupted",
                        "failure_kind": "uncertain-effect",
                        "uncertain": True,
                        "reason": "agent process restarted",
                    },
                }
            )
        )
    )
    with sessions.begin() as session:
        old_row = session.get(AgentOperation, old.id)
        old_job = session.get(Job, old_parent.id)
        node = session.get(AgentNode, NODE_A)
        assert old_row is not None and old_job is not None and node is not None
        old_row.retry_due_at = clock.now
        if exhausted:
            old_row.retry_disposition = None
            old_row.retry_disposition_attempt = None
            old_job.state = "waiting-for-operator"
        node.workload_intent_ordinal = 2
    new_parent = parent(sessions, clock)
    with sessions.begin() as session:
        new_job = session.get(Job, new_parent.id)
        assert new_job is not None
        new_job.payload = {"workload_intent_ordinal": 2}
        new_job.payload_digest = hashlib.sha256(
            canonical_message(new_job.payload)
        ).hexdigest()
        AgentJobService.request_superseded_workload_cancellation_in_session(
            session, [NODE_A], 2, clock.now
        )
    current = jobs.enqueue(new_parent.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    with sessions() as session:
        old_row = session.get(AgentOperation, old.id)
        old_job = session.get(Job, old_parent.id)
        assert old_row is not None and old_job is not None
        assert old_row.retry_disposition is None and old_row.retry_due_at is None
        assert (
            isinstance(old_job.result, dict)
            and old_job.result.get("cancel_requested") is True
        )
    claimed = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert claimed is not None and claimed.operation_id == current.id


@pytest.mark.parametrize(
    "terminal_state", ("succeeded", "failed", "waiting-for-operator", "expired")
)
def test_postgres_enqueue_rejects_terminal_parent(service, terminal_state: str) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, parent_job.id).state = terminal_state  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="terminal"):
        jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)


def test_postgres_enqueue_rejects_parent_commit_mismatch(service) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)

    with pytest.raises(ValueError, match="authority revision"):
        jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", "b" * 64, STOP_PAYLOAD)


def test_postgres_enqueue_rejects_node_outside_parent_targets(service) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, parent_job.id).targets = [NODE_A]  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="target"):
        jobs.enqueue(parent_job.id, NODE_B, "recipe.stop", COMMIT, STOP_PAYLOAD)


def test_postgres_enqueue_cannot_race_parent_finalization(
    service, postgres_engine
) -> None:
    sessions, clock = service
    finishing = AgentJobService(sessions, clock=clock)
    enqueueing = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    finishing.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    claim = claim_agent(finishing, NODE_A, "serial-a", 30)
    assert claim is not None
    aggregation_read = threading.Event()
    release = threading.Event()

    def pause_after_aggregation_read(
        _conn, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            threading.current_thread().name == "finisher"
            and "FROM agent_operations" in statement
            and "parent_job_id" in statement
            and "ORDER BY" in statement
        ):
            aggregation_read.set()
            assert release.wait(timeout=5)

    finish_errors: list[Exception] = []
    enqueue_errors: list[Exception] = []

    def finish() -> None:
        try:
            finishing.succeed(claim.fence, STOP_RESULT)
        except (
            AssertionError,
            OSError,
            RuntimeError,
            ValueError,
            SQLAlchemyError,
        ) as error:
            finish_errors.append(error)

    def enqueue() -> None:
        try:
            enqueueing.enqueue(
                parent_job.id, NODE_B, "recipe.stop", COMMIT, STOP_PAYLOAD
            )
        except (
            AssertionError,
            OSError,
            RuntimeError,
            ValueError,
            SQLAlchemyError,
        ) as error:
            enqueue_errors.append(error)

    event.listen(postgres_engine, "after_cursor_execute", pause_after_aggregation_read)
    try:
        finisher = threading.Thread(target=finish, name="finisher")
        enqueuer = threading.Thread(target=enqueue, name="enqueuer")
        finisher.start()
        assert aggregation_read.wait(timeout=5)
        enqueuer.start()
        enqueuer.join(timeout=2)
        assert not enqueuer.is_alive(), (
            "busy admission must release the caller promptly"
        )
        assert len(enqueue_errors) == 1
        assert isinstance(enqueue_errors[0], RunAdmissionBusy)
        release.set()
        finisher.join(timeout=5)
        enqueuer.join(timeout=5)
    finally:
        release.set()
        event.remove(
            postgres_engine, "after_cursor_execute", pause_after_aggregation_read
        )

    assert not finish_errors
    assert len(enqueue_errors) == 1
    assert isinstance(enqueue_errors[0], RunAdmissionBusy)
    assert state(sessions, parent_job.id) == "succeeded"
    # A fresh retry rechecks the completed parent instead of adding work to it.
    with pytest.raises(ValueError, match="terminal"):
        enqueueing.enqueue(parent_job.id, NODE_B, "recipe.stop", COMMIT, STOP_PAYLOAD)
    with sessions() as session:
        child_count = session.scalar(
            select(func.count())
            .select_from(AgentOperation)
            .where(
                AgentOperation.parent_job_id == parent_job.id,
            )
        )
    assert child_count == 1


def test_postgres_enqueue_locks_node_before_completion_and_parent_aggregation(
    service, postgres_engine
) -> None:
    sessions, clock = service
    enqueueing = AgentJobService(sessions, clock=clock)
    finishing = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    first_operation = finishing.enqueue(
        parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(finishing, NODE_A, "serial-a", 30)
    assert claim is not None
    node_locked = threading.Event()
    release_enqueue = threading.Event()
    enqueue_results: list[object] = []
    finish_errors: list[Exception] = []

    def pause_after_enqueue_node_lock(
        _conn, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            threading.current_thread().name == "enqueuer"
            and "FROM agent_nodes" in statement
            and "FOR UPDATE OF agent_nodes" in statement
        ):
            node_locked.set()
            assert release_enqueue.wait(timeout=5)

    def enqueue() -> None:
        try:
            enqueue_results.append(
                enqueueing.enqueue(
                    parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
                )
            )
        except (
            AssertionError,
            OSError,
            RuntimeError,
            ValueError,
            SQLAlchemyError,
        ) as error:
            enqueue_results.append(error)

    def finish() -> None:
        try:
            finishing.succeed(claim, STOP_RESULT)
        except (
            AssertionError,
            OSError,
            RuntimeError,
            ValueError,
            SQLAlchemyError,
        ) as error:
            finish_errors.append(error)

    event.listen(postgres_engine, "after_cursor_execute", pause_after_enqueue_node_lock)
    try:
        enqueuer = threading.Thread(target=enqueue, name="enqueuer")
        finisher = threading.Thread(target=finish, name="finisher")
        enqueuer.start()
        assert node_locked.wait(timeout=5)
        finisher.start()
        time.sleep(0.25)
        assert finisher.is_alive(), "completion must order behind enqueue's node lock"
        release_enqueue.set()
        enqueuer.join(timeout=5)
        finisher.join(timeout=5)
    finally:
        release_enqueue.set()
        event.remove(
            postgres_engine, "after_cursor_execute", pause_after_enqueue_node_lock
        )

    assert not enqueuer.is_alive() and not finisher.is_alive()
    assert not finish_errors
    assert len(enqueue_results) == 1
    assert not isinstance(enqueue_results[0], Exception)
    with sessions() as session:
        first = session.get(AgentOperation, first_operation.id)
        sibling_count = session.scalar(
            select(func.count())
            .select_from(AgentOperation)
            .where(
                AgentOperation.parent_job_id == parent_job.id,
            )
        )
        stored_parent = session.get(Job, parent_job.id)
        assert first is not None and first.state == "succeeded"
        assert sibling_count == 2
        assert stored_parent is not None and stored_parent.state == "queued"


def test_postgres_complete_serializes_expiry_gate_with_identity_lock(
    service, postgres_engine
) -> None:
    sessions, clock = service
    completing = AgentJobService(sessions, clock=clock)
    reclaiming = AgentJobService(sessions, clock=clock)
    completing.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    first = claim_agent(completing, NODE_A, "serial-a", 30)
    assert first is not None
    clock.advance(seconds=30)
    locked = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []
    reclaimed: list[object] = []

    def pause_after_operation_lock(
        _conn, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            threading.current_thread().name == "finisher"
            and "FROM agent_operations" in statement
            and "FOR UPDATE OF agent_operations" in statement
        ):
            locked.set()
            assert release.wait(timeout=5)

    event.listen(postgres_engine, "after_cursor_execute", pause_after_operation_lock)
    try:

        def finish() -> None:
            try:
                completing.succeed(first.fence, STOP_RESULT)
            except (
                AssertionError,
                OSError,
                RuntimeError,
                ValueError,
                SQLAlchemyError,
            ) as error:
                errors.append(error)

        def reclaim() -> None:
            try:
                reclaimed.append(claim_agent(reclaiming, NODE_A, "serial-a", 30))
            except (
                AssertionError,
                OSError,
                RuntimeError,
                ValueError,
                SQLAlchemyError,
            ) as error:
                reclaimed.append(error)

        finisher = threading.Thread(target=finish, name="finisher")
        reclaimer = threading.Thread(target=reclaim, name="reclaimer")
        finisher.start()
        assert locked.wait(timeout=5)
        reclaimer.start()
        time.sleep(0.25)
        assert reclaimer.is_alive(), (
            "reclaim must wait for the active identity transaction"
        )
        release.set()
        finisher.join(timeout=5)
        reclaimer.join(timeout=5)
    finally:
        release.set()
        event.remove(
            postgres_engine, "after_cursor_execute", pause_after_operation_lock
        )

    assert len(errors) == 1
    assert isinstance(errors[0], StaleAgentAttempt)
    assert not finisher.is_alive() and not reclaimer.is_alive()
    assert len(reclaimed) == 1
    assert not isinstance(reclaimed[0], Exception)
    assert reclaimed[0] is None
    with sessions() as session:
        assert (
            session.get(AgentOperation, first.operation_id).state
            == "waiting-for-operator"
        )


def test_postgres_concurrent_final_completions_aggregate_parent_once(
    service, postgres_engine
) -> None:
    sessions, clock = service
    first_service = AgentJobService(sessions, clock=clock)
    second_service = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    first_service.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    first_service.enqueue(parent_job.id, NODE_B, "recipe.stop", COMMIT, STOP_PAYLOAD)
    first = claim_agent(first_service, NODE_A, "serial-a", 30)
    second = claim_agent(second_service, NODE_B, "serial-b", 30)
    assert first is not None and second is not None
    aggregation_started = threading.Event()
    release = threading.Event()

    def pause_before_aggregation_reads_siblings(
        _conn, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            "FROM agent_operations" in statement
            and "parent_job_id" in statement
            and threading.current_thread().name in {"first-finisher", "second-finisher"}
        ):
            aggregation_started.set()
            assert release.wait(timeout=5)

    event.listen(
        postgres_engine, "after_cursor_execute", pause_before_aggregation_reads_siblings
    )
    errors: list[Exception] = []

    def complete(service, fence) -> None:
        try:
            service.succeed(fence, STOP_RESULT)
        except (
            AssertionError,
            OSError,
            RuntimeError,
            ValueError,
            SQLAlchemyError,
        ) as error:
            errors.append(error)

    try:
        thread_a = threading.Thread(
            target=complete, args=(first_service, first.fence), name="first-finisher"
        )
        thread_b = threading.Thread(
            target=complete, args=(second_service, second.fence), name="second-finisher"
        )
        thread_a.start()
        thread_b.start()
        assert aggregation_started.wait(timeout=5)
        time.sleep(0.25)
        release.set()
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)
    finally:
        event.remove(
            postgres_engine,
            "after_cursor_execute",
            pause_before_aggregation_reads_siblings,
        )

    assert not errors
    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert state(sessions, parent_job.id) == "succeeded"


@pytest.mark.parametrize("malformed", (1, "true"))
def test_postgres_non_boolean_cancel_flag_does_not_cancel(service, malformed) -> None:
    """A JSON value the SQL cast would read as true must not mean "cancelled".

    The canonical lifecycle result declares
    ``cancel_requested: Literal[True]``, so a stored ``1`` or ``"true"`` is
    malformed.  PostgreSQL's ``CAST(... ->> 'cancel_requested' AS BOOLEAN)``
    accepted both and silently cancelled the operation; the exact JSON read
    does not, and the admission path names the malformation instead of leaving
    it silent.
    """

    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    with sessions.begin() as session:
        job = session.get(Job, parent_job.id)
        assert job is not None
        job.result = {"cancel_requested": malformed}

    claim = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert claim is not None and claim.operation_id == operation.id

    with sessions() as session:
        job = session.get(Job, parent_job.id)
        assert job is not None and job.status_reason is not None
        assert "parent-cancel-flag-malformed" in job.status_reason


def test_postgres_boolean_cancel_request_is_named(service) -> None:
    """A genuine JSON ``true`` still cancels and is named by the classifier."""

    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    with sessions.begin() as session:
        job = session.get(Job, parent_job.id)
        assert job is not None
        job.result = {"cancel_requested": True}

    assert claim_agent(jobs, NODE_A, "serial-a", 30) is None

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.status_reason is not None
        assert "parent-cancel-requested" in stored.status_reason


def test_postgres_exhausted_exact_retry_rearms_once_and_has_one_claim_winner(service):
    sessions, clock = service
    first = AgentJobService(sessions, clock=clock)
    operation = first.enqueue(
        parent(sessions, clock).id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    original = claim_agent(first, NODE_A, "serial-a", 30)
    assert original is not None
    clock.advance(seconds=31)
    assert claim_agent(first, NODE_A, "serial-a", 30) is None
    with sessions.begin() as session:
        parked = session.get(AgentOperation, operation.id)
        assert parked is not None
        parked.current_attempt = 5
        parked.retry_disposition = None
        parked.retry_disposition_attempt = None
        parked.retry_due_at = None
        previous = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == original.fence
            )
        )
        assert previous is not None and previous.state == "expired"
        previous.attempt = 5
        job = session.get(Job, parked.parent_job_id)
        assert job is not None
        job.state = "waiting-for-operator"
    capabilities = [
        "agent.runtime.rust.v1",
        "recipe.stop",
        "agent.lifecycle.resume.exact.v1",
    ]
    services = (
        AgentJobService(sessions, clock=clock),
        AgentJobService(sessions, clock=clock),
    )
    barrier = threading.Barrier(2)

    def claim_from(service):
        barrier.wait(timeout=5)
        return claim_agent(
            service,
            NODE_A,
            "serial-a",
            30,
            protocol_version=3,
            capabilities=capabilities,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(claim_from, services)) == [None, None]
    with sessions() as session:
        parked = session.get(AgentOperation, operation.id)
        assert parked is not None and parked.retry_due_at is not None
        assert parked.current_attempt == 5
        due = parked.retry_due_at
    clock.now = due
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim_from, services))
    resumed = [claim for claim in outcomes if claim is not None]
    assert len(resumed) == 1 and resumed[0].attempt == 6
    assert resumed[0].operation_id == operation.id
    with pytest.raises(StaleAgentAttempt):
        first.succeed(original, STOP_RESULT)
    services[0].succeed(resumed[0], STOP_RESULT)
    assert state(sessions, original.job_id) == "succeeded"
