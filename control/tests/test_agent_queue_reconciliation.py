from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import AgentResult
from vonk_control.agent_jobs import AgentJobService, StaleAgentAttempt
from vonk_control.jobs import JobService
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
    Reconciliation,
    ReconciliationOperation,
)

NODE_ID = "spk_" + "a" * 32
COMMIT = "a"  * 64


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 5, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def queue(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'queue.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = Clock()
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_ID, state="active", capabilities=[]))
        session.add(
            AgentCertificate(
                serial="serial-a",
                node_id=NODE_ID,
                not_before=clock.now - timedelta(seconds=1),
                not_after=clock.now + timedelta(hours=1),
                fingerprint="fingerprint-a",
            )
        )
        session.add(_parent(clock))
    return sessions, clock


def _parent(clock: Clock) -> Job:
    return Job(
        id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_ID],
        payload_digest=hashlib.sha256(b"{}").hexdigest(),
        payload={},
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )


def _parent_id(sessions: sessionmaker[Session]) -> str:
    with sessions() as session:
        return session.scalars(select(Job.id)).one()


def _claim(service: AgentJobService):
    claim = service.claim(NODE_ID, "serial-a", 30)
    assert claim is not None
    return claim


def _link_reconciliation(
    sessions: sessionmaker[Session], clock: Clock, parent_id: str
) -> str:
    reconciliation_id = str(uuid.uuid4())
    with sessions.begin() as session:
        session.add(
            Reconciliation(
                id=reconciliation_id,
                authority_revision=COMMIT,
                status="planned",
                summary={},
                created_at=clock.now,
            )
        )
        parent = session.get(Job, parent_id)
        assert parent is not None
        parent.reconciliation_id = reconciliation_id
    return reconciliation_id


def _result(claim, state: str, result: dict[str, object]) -> AgentResult:
    return AgentResult(
        schema_version=1,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=claim.fence,
        node_id=claim.node_id,
        deadline=claim.deadline,
        state=state,
        result=result,
    )


def test_session_enqueue_uses_caller_operation_id_and_caller_transaction(queue) -> None:
    """A missing session primitive would commit independently or replace the UUID."""
    sessions, clock = queue
    service = AgentJobService(sessions, clock=clock)
    operation_id = str(uuid.uuid4())

    with pytest.raises(RuntimeError, match="rollback"), sessions.begin() as session:
        stored = service.enqueue_in_session(
            session,
            _parent_id(sessions),
            NODE_ID,
            "workload.stop",
            COMMIT,
            {"workload_id": "model"},
            operation_id=operation_id,
        )
        assert stored.id == operation_id
        assert session.get(AgentOperation, operation_id) is stored
        raise RuntimeError("rollback")

    with sessions() as session:
        assert session.get(AgentOperation, operation_id) is None


def test_result_consumer_can_be_late_bound_exactly_once_before_activity(queue) -> None:
    """Silent replacement would let a different projection consume later results."""
    sessions, clock = queue
    received: list[AgentResult] = []
    service = AgentJobService(sessions, clock=clock)
    service.set_result_consumer(
        lambda _session, _operation, _attempt, message: received.append(message)
    )

    with pytest.raises(RuntimeError, match="already configured"):
        service.set_result_consumer(
            lambda _session, _operation, _attempt, message: received.append(message)
        )

    service.enqueue(
        _parent_id(sessions),
        NODE_ID,
        "workload.stop",
        COMMIT,
        {"workload_id": "model"},
    )
    claim = _claim(service)
    message = _result(
        claim,
        "failed",
        {"status": "failed", "error_code": "service_failed"},
    )
    service.record_result(message)
    assert received == [message]


@pytest.mark.parametrize("consumer", (None, object()))
def test_result_consumer_late_binding_rejects_noncallable(queue, consumer) -> None:
    """Accepting an unusable hook defers a configuration error into result commit."""
    sessions, clock = queue
    service = AgentJobService(sessions, clock=clock)

    with pytest.raises(TypeError, match="callable"):
        service.set_result_consumer(consumer)


def test_result_consumer_constructor_rejects_noncallable(queue) -> None:
    """An invalid startup hook must fail before the queue begins serving work."""
    sessions, clock = queue

    with pytest.raises(TypeError, match="callable"):
        AgentJobService(sessions, clock=clock, result_consumer=object())  # type: ignore[arg-type]


@pytest.mark.parametrize("activity", ("enqueue", "claim", "result"))
def test_result_consumer_cannot_be_bound_after_queue_activity(queue, activity: str) -> None:
    """A hook installed after activity can miss a result and split projection authority."""
    sessions, clock = queue
    service = AgentJobService(sessions, clock=clock)
    if activity == "enqueue":
        service.enqueue(
            _parent_id(sessions),
            NODE_ID,
            "workload.stop",
            COMMIT,
            {"workload_id": "model"},
        )
    elif activity == "claim":
        assert service.claim(NODE_ID, "serial-a", 30) is None
    else:
        bootstrap = AgentJobService(sessions, clock=clock)
        bootstrap.enqueue(
            _parent_id(sessions),
            NODE_ID,
            "workload.stop",
            COMMIT,
            {"workload_id": "model"},
        )
        claim = _claim(bootstrap)
        service.record_result(
            _result(
                claim,
                "failed",
                {"status": "failed", "error_code": "service_failed"},
            )
        )

    with pytest.raises(RuntimeError, match="already started"):
        service.set_result_consumer(lambda *_args: None)


@pytest.mark.parametrize(
    ("state", "result"),
    (
        (
            "succeeded",
            {
                "status": "ok",
                "evidence": {
                    "action": "stop",
                    "workload_id": "model",
                    "evidence_digest": "e" * 64,
                },
            },
        ),
        ("failed", {"status": "failed", "error_code": "service_failed"}),
        (
            "waiting-for-operator",
            {"status": "waiting-for-operator", "reason": "inspect_console"},
        ),
    ),
)
def test_result_consumer_receives_exact_canonical_message_in_finish_transaction(
    queue, state: str, result: dict[str, object]
) -> None:
    """Discarding failure/wait documents or invoking after commit breaks projection."""
    sessions, clock = queue
    received: list[AgentResult] = []

    def consume(
        session: Session,
        operation: AgentOperation,
        attempt: AgentOperationAttempt,
        message: AgentResult,
    ) -> None:
        assert session.in_transaction()
        assert operation.state == state
        assert attempt.state == state
        assert attempt.result == result
        received.append(message)
        parent = session.get(Job, operation.parent_job_id)
        assert parent is not None
        parent.result = {"consumed_fence": message.fence}

    service = AgentJobService(sessions, clock=clock, result_consumer=consume)
    service.enqueue(
        _parent_id(sessions),
        NODE_ID,
        "workload.stop",
        COMMIT,
        {"workload_id": "model"},
    )
    claim = _claim(service)
    message = _result(claim, state, result)

    service.record_result(message)

    assert received == [message]
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == claim.operation_id
            )
        )
        parent = session.get(Job, claim.job_id)
        assert attempt is not None and attempt.result == result
        assert parent is not None
        assert parent.result == {"consumed_fence": claim.fence}


def test_consumer_rejection_rolls_back_agent_result_and_parent_projection(
    queue,
) -> None:
    """Calling a bad-digest consumer outside the transaction would retain the result."""
    sessions, clock = queue

    def reject_digest(
        session: Session,
        operation: AgentOperation,
        attempt: AgentOperationAttempt,
        message: AgentResult,
    ) -> None:
        parent = session.get(Job, operation.parent_job_id)
        assert parent is not None
        parent.result = {"must": "rollback"}
        raise ValueError("result digest does not match execution projection")

    service = AgentJobService(sessions, clock=clock, result_consumer=reject_digest)
    service.enqueue(
        _parent_id(sessions),
        NODE_ID,
        "workload.stop",
        COMMIT,
        {"workload_id": "model"},
    )
    claim = _claim(service)

    with pytest.raises(ValueError, match="digest"):
        service.record_result(
            _result(
                claim,
                "succeeded",
                {"status": "ok", "evidence": {"evidence_digest": "f" * 64}},
            )
        )

    with sessions() as session:
        operation = session.get(AgentOperation, claim.operation_id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == claim.operation_id
            )
        )
        parent = session.get(Job, claim.job_id)
        assert operation is not None and operation.state == "running"
        assert attempt is not None and attempt.state == "running"
        assert attempt.result is None
        assert parent is not None and parent.result is None


def test_linked_reconciliation_job_bypasses_generic_parent_terminalization(
    queue,
) -> None:
    """First-wave completion must not mark an orchestrated parent terminal."""
    sessions, clock = queue
    parent_id = _parent_id(sessions)
    reconciliation_id = _link_reconciliation(sessions, clock, parent_id)
    service = AgentJobService(sessions, clock=clock)
    operation = service.enqueue(
        parent_id,
        NODE_ID,
        "workload.stop",
        COMMIT,
        {"workload_id": "model"},
    )
    with sessions.begin() as session:
        reconciliation = session.get(Reconciliation, reconciliation_id)
        parent = session.get(Job, parent_id)
        assert reconciliation is not None and parent is not None
        reconciliation.status = "running"
        reconciliation.current_phase = "dispatching"
        parent.state = "running"
        session.add(
            ReconciliationOperation(
                reconciliation_id=reconciliation_id,
                graph_operation_id="model:stop",
                role="primary",
                agent_operation_id=operation.id,
                expected_payload_digest=operation.payload_digest,
                state="queued",
            )
        )
    claim = _claim(service)

    service.record_result(
        _result(
            claim,
            "succeeded",
            {"status": "ok", "evidence": {"evidence_digest": "e" * 64}},
        )
    )

    with sessions() as session:
        parent = session.get(Job, parent_id)
        operation = session.get(AgentOperation, claim.operation_id)
        assert operation is not None and operation.state == "succeeded"
        assert parent is not None and parent.state == "running"


def test_generic_worker_claim_skips_jobs_linked_to_reconciliation(queue) -> None:
    """A generic worker claiming the linked parent races the orchestrator."""
    sessions, clock = queue
    linked_id = _parent_id(sessions)
    _link_reconciliation(sessions, clock, linked_id)
    with sessions.begin() as session:
        linked = session.get(Job, linked_id)
        assert linked is not None
        linked.created_at = clock.now - timedelta(seconds=1)
    jobs = JobService(sessions, clock=clock)
    ordinary = jobs.enqueue("probe", "operator", COMMIT, [NODE_ID], {})

    claim = jobs.claim("generic-worker", 30)

    assert claim is not None and claim.job_id == ordinary.id
    with sessions() as session:
        linked = session.get(Job, linked_id)
        assert linked is not None and linked.state == "queued"


@pytest.mark.parametrize("invalidity", ("stale", "revoked", "bad-fence"))
def test_invalid_result_is_no_write_and_never_reaches_consumer(
    queue, invalidity: str
) -> None:
    """Relaxing the active fence checks would unlock reconciliation state."""
    sessions, clock = queue
    consumed: list[AgentResult] = []
    service = AgentJobService(
        sessions,
        clock=clock,
        result_consumer=lambda _session, _operation, _attempt, message: consumed.append(
            message
        ),
    )
    service.enqueue(
        _parent_id(sessions),
        NODE_ID,
        "workload.stop",
        COMMIT,
        {"workload_id": "model"},
    )
    claim = _claim(service)
    message = _result(claim, "failed", {"status": "failed", "error_code": "failed"})
    if invalidity == "stale":
        clock.now += timedelta(seconds=31)
    elif invalidity == "revoked":
        with sessions.begin() as session:
            certificate = session.get(AgentCertificate, "serial-a")
            assert certificate is not None
            certificate.revoked_at = clock.now
            certificate.state = "revoked"
    else:
        message = AgentResult(**{**message.__dict__, "fence": str(uuid.uuid4())})

    with pytest.raises(StaleAgentAttempt):
        service.record_result(message)

    assert consumed == []
    with sessions() as session:
        operation = session.get(AgentOperation, claim.operation_id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == claim.operation_id
            )
        )
        assert operation is not None and operation.state == "running"
        assert attempt is not None and attempt.state == "running"
        assert attempt.result is None
