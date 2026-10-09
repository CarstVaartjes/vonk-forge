"""One complete actual worker observation drives the stable update authority."""

from datetime import UTC, datetime, timedelta
from typing import NoReturn

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_agent_protocol import canonical_message
from vonk_control import platform_observation
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.models import Base, ControlProcessHeartbeat
from vonk_control.platform_observation import PlatformObservation, PlatformObserver
from vonk_control.worker import WorkerHeartbeatRecorder

from cluster_profiles import runtime_identity
from cluster_profiles.runtime_identity import RuntimeBuildIdentity


class Jobs:
    def enqueue(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("compatibility reads cannot enqueue")

    def get(self, job_id: str) -> object:
        raise KeyError(job_id)


def test_authenticated_contract_complete_membership_fault_recovery(
    monkeypatch,
):
    # This proves committed producer/store/capture semantics, not crash durability.
    # Keep all 257 real process recorders and separate transactions without
    # charging hundreds of disk fsyncs to an observation-contract test.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = [datetime(2026, 10, 7, tzinfo=UTC)]
    identity_reads = []
    api_identity = RuntimeBuildIdentity("a" * 40, "c" * 64, "d" * 64)
    worker_identity = [RuntimeBuildIdentity("b" * 40, "c" * 64, "d" * 64)]

    def read_api_identity():
        identity_reads.append(True)
        return api_identity

    monkeypatch.setattr(
        platform_observation, "packaged_runtime_identity", read_api_identity
    )
    monkeypatch.setattr(
        runtime_identity, "packaged_runtime_identity", lambda: worker_identity[0]
    )
    observer = PlatformObserver(sessions, clock=lambda: clock[0])
    codec = TokenCodec(b"k" * 32)
    token = codec.issue(Actor("viewer", "viewer"), ttl_seconds=100, now=0)
    peer = TestClient(
        create_app(
            jobs=Jobs(), tokens=codec, now=lambda: 10, platform_observer=observer
        )
    )
    assert peer.get("/api/cli/contract").status_code == 401
    assert identity_reads == []

    def read():
        before = len(identity_reads)
        response = peer.get(
            "/api/cli/contract", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert len(identity_reads) == before + 1
        return response.json()

    empty = read()
    assert empty["worker_compatibility"] == "unknown"
    assert empty["worker_count"] == 0
    recorders = [
        WorkerHeartbeatRecorder(
            sessions, process_instance_id=f"{index:064x}", clock=lambda: clock[0]
        )
        for index in range(257)
    ]
    for recorder in recorders:
        recorder.completed_loop()
    complete = read()
    assert complete["worker_count"] == 257
    assert complete["worker_source_sha"] == "b" * 40
    assert complete["api"]["source_sha"] == "a" * 40
    assert complete["worker_compatibility"] == "compatible"
    damaged_identity = "unreadable-stored-worker"
    original_identity = f"{256:064x}"
    # Simulate damaged stored bytes in this isolated SQLite fixture, not an
    # authorized write through the production constraints. Restore constraint
    # enforcement before either capture or the subsequent repair observes it.
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        try:
            connection.execute(
                update(ControlProcessHeartbeat)
                .where(ControlProcessHeartbeat.process_instance_id == original_identity)
                .values(process_instance_id=damaged_identity)
            )
        finally:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
        assert (
            connection.exec_driver_sql("PRAGMA ignore_check_constraints").scalar_one()
            == 0
        )
    for endpoint in ("/api/cli/contract", "/api/platform"):
        failure = peer.get(endpoint, headers={"Authorization": f"Bearer {token}"})
        assert failure.status_code == 503
        assert failure.headers["Retry-After"] == "5"
        assert failure.headers["Cache-Control"] == "no-store"
        problem = failure.json()
        assert damaged_identity not in failure.text
        assert "workers" not in problem and "worker_count" not in problem
    with sessions.begin() as session:
        session.execute(
            update(ControlProcessHeartbeat)
            .where(ControlProcessHeartbeat.process_instance_id == damaged_identity)
            .values(process_instance_id=original_identity)
        )
    assert read()["worker_compatibility"] == "compatible"
    repaired_platform = peer.get(
        "/api/platform", headers={"Authorization": f"Bearer {token}"}
    )
    assert repaired_platform.status_code == 200
    # The same failed observation re-captures its complete membership after the
    # exact stored-row repair; a healthy sibling route is not repair evidence.
    from tests.observation_transfer_peer import observation_document

    repaired_observation = PlatformObservation.model_validate_json(
        canonical_message(observation_document(repaired_platform)), strict=True
    )
    assert repaired_observation.workers is not None
    assert len(repaired_observation.workers) == 257
    worker_identity[0] = RuntimeBuildIdentity("e" * 40, "c" * 64, "d" * 64)
    mixed_process = WorkerHeartbeatRecorder(
        sessions, process_instance_id=f"{257:064x}", clock=lambda: clock[0]
    )
    mixed_process.completed_loop()
    mixed = read()
    assert mixed["worker_compatibility"] == "unknown"
    assert mixed["worker_issue"] == "worker-source-mixed"
    assert mixed["worker_membership_sha256"] != complete["worker_membership_sha256"]
    # An existing process identity is immutable. Let the foreign process cease
    # renewing while the original compatible processes keep completing loops.
    clock[0] += timedelta(seconds=31)
    for recorder in recorders:
        recorder.completed_loop()
    repaired = read()
    assert repaired["worker_compatibility"] == "compatible"
    assert repaired["worker_count"] == 257
    clock[0] += timedelta(seconds=31)
    expired = read()
    assert expired["worker_compatibility"] == "unknown"
    assert expired["worker_count"] == 0
