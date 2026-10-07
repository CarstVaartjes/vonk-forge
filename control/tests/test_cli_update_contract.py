"""One complete actual worker observation drives the stable update authority."""

from datetime import UTC, datetime, timedelta
from typing import NoReturn

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker
from vonk_control import platform_observation
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.models import Base, ControlProcessHeartbeat
from vonk_control.platform_observation import PlatformObserver
from vonk_control.worker import WorkerHeartbeatRecorder

from cluster_profiles import runtime_identity
from cluster_profiles.runtime_identity import RuntimeBuildIdentity


class Jobs:
    def enqueue(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("compatibility reads cannot enqueue")

    def get(self, job_id: str) -> object:
        raise KeyError(job_id)


def test_authenticated_contract_complete_membership_fault_recovery(
    tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'compatibility.sqlite'}")
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
    with sessions.begin() as session:
        session.execute(
            update(ControlProcessHeartbeat)
            .where(ControlProcessHeartbeat.process_instance_id == original_identity)
            .values(process_instance_id=damaged_identity)
        )
    for endpoint in ("/api/cli/contract", "/api/platform"):
        failure = peer.get(endpoint, headers={"Authorization": f"Bearer {token}"})
        assert failure.status_code == 503
        assert failure.headers["Retry-After"] == "5"
        assert failure.headers["Cache-Control"] == "no-store"
        problem = failure.json()
        assert problem["context"]["decision"] == "retry"
        assert problem["context"]["retryable"] is True
        assert problem["context"]["source"] == "unknown"
        assert "stored-worker-validation" in problem["detail"]
        assert damaged_identity not in failure.text
        assert "workers" not in problem and "worker_count" not in problem
    with sessions.begin() as session:
        session.execute(
            update(ControlProcessHeartbeat)
            .where(ControlProcessHeartbeat.process_instance_id == damaged_identity)
            .values(process_instance_id=original_identity)
        )
    assert read()["worker_compatibility"] == "compatible"
    worker_identity[0] = RuntimeBuildIdentity("e" * 40, "c" * 64, "d" * 64)
    recorders[-1].completed_loop()
    mixed = read()
    assert mixed["worker_compatibility"] == "unknown"
    assert mixed["worker_issue"] == "worker-source-mixed"
    assert mixed["worker_membership_sha256"] != complete["worker_membership_sha256"]
    worker_identity[0] = RuntimeBuildIdentity("b" * 40, "c" * 64, "d" * 64)
    recorders[-1].completed_loop()
    assert read()["worker_compatibility"] == "compatible"
    clock[0] += timedelta(seconds=31)
    expired = read()
    assert expired["worker_compatibility"] == "unknown"
    assert expired["worker_count"] == 0
