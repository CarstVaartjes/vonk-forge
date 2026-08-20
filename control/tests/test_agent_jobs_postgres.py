from __future__ import annotations

import hashlib
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
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

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
COMMIT = "a"  * 64
PROBE_RESULT = {
    "status": "ok",
    "evidence": {
        "vonk_forge": {
            "schema_version": 1,
            "memory": {"available_bytes": 1_000},
            "storage": {"available_bytes": 2_000},
            "accelerator": {"available": True},
        },
        "nvidia": {"tools": {}},
    },
}


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


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for PostgreSQL locking integration tests")
    try:
        container = subprocess.check_output([
            "docker", "run", "--rm", "-d", "-e", "POSTGRES_PASSWORD=postgres",
            "-p", "127.0.0.1::5432", "postgres:16",
        ], text=True).strip()
    except subprocess.CalledProcessError as error:
        pytest.skip(f"disposable PostgreSQL is unavailable: {error}")
    try:
        port = subprocess.check_output([
            "docker", "inspect", "-f",
            "{{(index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostPort}}", container,
        ], text=True).strip()
        engine = create_engine(f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/postgres")
        for _ in range(100):
            try:
                with engine.connect():
                    break
            except (OSError, SQLAlchemyError):
                time.sleep(0.1)
        else:
            pytest.skip("disposable PostgreSQL did not become ready")
        yield engine
        engine.dispose()
    finally:
        subprocess.run(["docker", "stop", container], check=False, capture_output=True)


@pytest.fixture
def service(postgres_engine):
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    clock = Clock()
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
        session.flush()
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(AgentCertificate(
                serial=serial,
                node_id=node_id,
                not_before=clock.now - timedelta(seconds=1),
                not_after=clock.now + timedelta(hours=1),
                fingerprint=f"fingerprint-{serial}",
            ))
    return sessions, clock


def parent(sessions, clock) -> Job:
    job = Job(
        request_id=str(uuid.uuid4()), kind="agent.operations", state="queued", actor="operator",
        authority_revision=COMMIT, targets=[NODE_A, NODE_B], payload_digest=hashlib.sha256(b"{}").hexdigest(),
        payload={}, current_attempt=0, created_at=clock.now, updated_at=clock.now,
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
    with sessions.begin() as session:
        durable = session.get(Job, job.id)
        assert durable is not None
        durable.state = "waiting-for-operator"
        durable.status_reason = "operator approval required"
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


def test_postgres_claim_locks_only_operations_without_nullable_join(service, postgres_engine) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if "FROM agent_operations" in statement and "FOR UPDATE" in statement:
            statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", record)
    try:
        assert jobs.claim(NODE_A, "serial-a", 30) is not None
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)

    assert len(statements) == 1
    assert "LEFT OUTER JOIN" not in statements[0]
    assert "FOR UPDATE OF agent_operations SKIP LOCKED" in statements[0]


def test_postgres_separate_services_cannot_claim_the_same_operation(service) -> None:
    sessions, clock = service
    first_service = AgentJobService(sessions, clock=clock)
    second_service = AgentJobService(sessions, clock=clock)
    operation = first_service.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    barrier = threading.Barrier(2)

    def claim(service):
        barrier.wait()
        return service.claim(NODE_A, "serial-a", 30)

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
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    claim = None
    original_deadline = None
    if agent_action != "claim":
        claim = jobs.claim(NODE_A, "serial-a", 30, protocol_version=3)
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
        except (AssertionError, OSError, RuntimeError, ValueError, SQLAlchemyError) as error:
            revocation_errors.append(error)

    def act() -> None:
        try:
            if agent_action == "claim":
                action_results.append(jobs.claim(NODE_A, "serial-a", 30))
            elif agent_action == "heartbeat":
                assert claim is not None
                action_results.append(jobs.heartbeat(claim, {"phase": "checking"}, 60))
            else:
                assert claim is not None
                jobs.succeed(claim, PROBE_RESULT)
                action_results.append(None)
        except (AssertionError, OSError, RuntimeError, ValueError, SQLAlchemyError) as error:
            action_results.append(error)

    event.listen(postgres_engine, "after_cursor_execute", pause_after_node_lock)
    try:
        revoker = threading.Thread(target=revoke, name="revoker")
        worker = threading.Thread(target=act, name="agent-worker")
        revoker.start()
        assert revocation_locked.wait(timeout=5)
        worker.start()
        time.sleep(0.25)
        assert worker.is_alive(), "agent work must wait for the revocation identity lock"
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
        assert node is not None and node.state == "retired" and node.last_seen_at is None
        assert certificate is not None and certificate.state == "revoked"
        assert stored_operation is not None
        if agent_action == "claim":
            assert stored_operation.state == "queued"
            assert stored_operation.current_attempt == 0
        else:
            assert claim is not None
            attempt = session.scalar(select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == claim.attempt,
            ))
            assert attempt is not None and attempt.state == "running"
            assert attempt.lease_deadline.astimezone(UTC) == original_deadline
            assert attempt.progress is None and attempt.result is None


@pytest.mark.parametrize("operation_kind", ("node.probe", "workload.health", "workload.verify"))
def test_postgres_expired_safe_operation_is_automatically_reclaimed(service, operation_kind: str) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    jobs.enqueue(parent(sessions, clock).id, NODE_A, operation_kind, COMMIT, {})
    first = jobs.claim(NODE_A, "serial-a", 30)
    assert first is not None

    clock.advance(seconds=30)
    second = jobs.claim(NODE_A, "serial-a", 30)

    assert second is not None
    assert second.operation_id == first.operation_id
    assert second.attempt == 2


@pytest.mark.parametrize("operation_kind", ("release.install", "workload.start"))
def test_postgres_expired_mutating_operation_requires_persisted_retry_disposition(
    service, operation_kind: str
) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, operation_kind, COMMIT, {})
    first = jobs.claim(NODE_A, "serial-a", 30)
    assert first is not None

    clock.advance(seconds=30)
    assert jobs.claim(NODE_A, "serial-a", 30) is None
    with sessions() as session:
        gated = session.get(AgentOperation, operation.id)
        assert gated is not None
        assert gated.state == "waiting-for-operator"
        assert gated.retry_disposition is None
        assert gated.retry_disposition_attempt is None
        attempt = session.scalar(select(AgentOperationAttempt).where(
            AgentOperationAttempt.operation_id == operation.id,
            AgentOperationAttempt.attempt == 1,
        ))
        assert attempt is not None and attempt.state == "expired"
        assert session.get(Job, parent_job.id).state == "waiting-for-operator"  # type: ignore[union-attr]

    with sessions.begin() as session:
        gated = session.get(AgentOperation, operation.id)
        assert gated is not None
        gated.retry_disposition = "retry"
        gated.retry_disposition_attempt = 1

    second = jobs.claim(NODE_A, "serial-a", 30)
    assert second is not None
    assert second.operation_id == first.operation_id
    assert second.attempt == 2

    clock.advance(seconds=30)
    assert jobs.claim(NODE_A, "serial-a", 30) is None


@pytest.mark.parametrize("terminal_state", ("succeeded", "failed", "waiting-for-operator", "expired"))
def test_postgres_enqueue_rejects_terminal_parent(service, terminal_state: str) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, parent_job.id).state = terminal_state  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="terminal"):
        jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})


def test_postgres_enqueue_rejects_parent_commit_mismatch(service) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)

    with pytest.raises(ValueError, match="authority revision"):
        jobs.enqueue(parent_job.id, NODE_A, "node.probe", "b"  * 64, {})


def test_postgres_enqueue_rejects_node_outside_parent_targets(service) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, parent_job.id).targets = [NODE_A]  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="target"):
        jobs.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})


def test_postgres_enqueue_cannot_race_parent_finalization(service, postgres_engine) -> None:
    sessions, clock = service
    finishing = AgentJobService(sessions, clock=clock)
    enqueueing = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    finishing.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
    claim = finishing.claim(NODE_A, "serial-a", 30)
    assert claim is not None
    aggregation_read = threading.Event()
    release = threading.Event()

    def pause_after_aggregation_read(_conn, _cursor, statement, _parameters, _context, _many) -> None:
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
            finishing.succeed(claim.fence, PROBE_RESULT)
        except (AssertionError, OSError, RuntimeError, ValueError, SQLAlchemyError) as error:
            finish_errors.append(error)

    def enqueue() -> None:
        try:
            enqueueing.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})
        except (AssertionError, OSError, RuntimeError, ValueError, SQLAlchemyError) as error:
            enqueue_errors.append(error)

    event.listen(postgres_engine, "after_cursor_execute", pause_after_aggregation_read)
    try:
        finisher = threading.Thread(target=finish, name="finisher")
        enqueuer = threading.Thread(target=enqueue, name="enqueuer")
        finisher.start()
        assert aggregation_read.wait(timeout=5)
        enqueuer.start()
        time.sleep(0.25)
        assert enqueuer.is_alive(), "enqueue must wait for the parent row lock"
        release.set()
        finisher.join(timeout=5)
        enqueuer.join(timeout=5)
    finally:
        release.set()
        event.remove(postgres_engine, "after_cursor_execute", pause_after_aggregation_read)

    assert not finish_errors
    assert len(enqueue_errors) == 1
    assert isinstance(enqueue_errors[0], ValueError)
    assert "terminal" in str(enqueue_errors[0])
    assert state(sessions, parent_job.id) == "succeeded"
    with sessions() as session:
        child_count = session.scalar(select(func.count()).select_from(AgentOperation).where(
            AgentOperation.parent_job_id == parent_job.id,
        ))
    assert child_count == 1


def test_postgres_enqueue_locks_node_before_completion_and_parent_aggregation(
    service, postgres_engine
) -> None:
    sessions, clock = service
    enqueueing = AgentJobService(sessions, clock=clock)
    finishing = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    first_operation = finishing.enqueue(
        parent_job.id, NODE_A, "node.probe", COMMIT, {}
    )
    claim = finishing.claim(NODE_A, "serial-a", 30)
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
                    parent_job.id, NODE_A, "workload.health", COMMIT, {}
                )
            )
        except (AssertionError, OSError, RuntimeError, ValueError, SQLAlchemyError) as error:
            enqueue_results.append(error)

    def finish() -> None:
        try:
            finishing.succeed(claim, PROBE_RESULT)
        except (AssertionError, OSError, RuntimeError, ValueError, SQLAlchemyError) as error:
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
        event.remove(postgres_engine, "after_cursor_execute", pause_after_enqueue_node_lock)

    assert not enqueuer.is_alive() and not finisher.is_alive()
    assert not finish_errors
    assert len(enqueue_results) == 1
    assert not isinstance(enqueue_results[0], Exception)
    with sessions() as session:
        first = session.get(AgentOperation, first_operation.id)
        sibling_count = session.scalar(select(func.count()).select_from(AgentOperation).where(
            AgentOperation.parent_job_id == parent_job.id,
        ))
        stored_parent = session.get(Job, parent_job.id)
        assert first is not None and first.state == "succeeded"
        assert sibling_count == 2
        assert stored_parent is not None and stored_parent.state == "queued"


def test_postgres_complete_serializes_expired_reclaim_with_identity_lock(
    service, postgres_engine
) -> None:
    sessions, clock = service
    completing = AgentJobService(sessions, clock=clock)
    reclaiming = AgentJobService(sessions, clock=clock)
    completing.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    first = completing.claim(NODE_A, "serial-a", 30)
    assert first is not None
    clock.advance(seconds=30)
    locked = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []
    reclaimed: list[object] = []

    def pause_after_operation_lock(_conn, _cursor, statement, _parameters, _context, _many) -> None:
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
                completing.succeed(first.fence, PROBE_RESULT)
            except (AssertionError, OSError, RuntimeError, ValueError, SQLAlchemyError) as error:
                errors.append(error)

        def reclaim() -> None:
            try:
                reclaimed.append(reclaiming.claim(NODE_A, "serial-a", 30))
            except (AssertionError, OSError, RuntimeError, ValueError, SQLAlchemyError) as error:
                reclaimed.append(error)

        finisher = threading.Thread(target=finish, name="finisher")
        reclaimer = threading.Thread(target=reclaim, name="reclaimer")
        finisher.start()
        assert locked.wait(timeout=5)
        reclaimer.start()
        time.sleep(0.25)
        assert reclaimer.is_alive(), "reclaim must wait for the active identity transaction"
        release.set()
        finisher.join(timeout=5)
        reclaimer.join(timeout=5)
    finally:
        release.set()
        event.remove(postgres_engine, "after_cursor_execute", pause_after_operation_lock)

    assert len(errors) == 1
    assert isinstance(errors[0], StaleAgentAttempt)
    assert not finisher.is_alive() and not reclaimer.is_alive()
    assert len(reclaimed) == 1
    assert not isinstance(reclaimed[0], Exception)
    assert reclaimed[0] is not None


def test_postgres_concurrent_final_completions_aggregate_parent_once(service, postgres_engine) -> None:
    sessions, clock = service
    first_service = AgentJobService(sessions, clock=clock)
    second_service = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    first_service.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
    first_service.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})
    first = first_service.claim(NODE_A, "serial-a", 30)
    second = second_service.claim(NODE_B, "serial-b", 30)
    assert first is not None and second is not None
    aggregation_started = threading.Event()
    release = threading.Event()

    def pause_before_aggregation_reads_siblings(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if (
            "FROM agent_operations" in statement
            and "parent_job_id" in statement
            and threading.current_thread().name in {"first-finisher", "second-finisher"}
        ):
            aggregation_started.set()
            assert release.wait(timeout=5)

    event.listen(postgres_engine, "after_cursor_execute", pause_before_aggregation_reads_siblings)
    errors: list[Exception] = []
    def complete(service, fence) -> None:
        try:
            service.succeed(fence, PROBE_RESULT)
        except (AssertionError, OSError, RuntimeError, ValueError, SQLAlchemyError) as error:
            errors.append(error)
    try:
        thread_a = threading.Thread(target=complete, args=(first_service, first.fence), name="first-finisher")
        thread_b = threading.Thread(target=complete, args=(second_service, second.fence), name="second-finisher")
        thread_a.start(); thread_b.start()
        assert aggregation_started.wait(timeout=5)
        time.sleep(0.25)
        release.set()
        thread_a.join(timeout=5); thread_b.join(timeout=5)
    finally:
        event.remove(postgres_engine, "after_cursor_execute", pause_before_aggregation_reads_siblings)

    assert not errors
    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert state(sessions, parent_job.id) == "succeeded"
