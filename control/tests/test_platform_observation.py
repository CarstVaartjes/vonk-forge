"""The actual worker producer, authenticated API, and native CLI read one contract."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from email.message import Message
from typing import NoReturn, Self

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control import platform_observation
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.models import Base, ControlProcessHeartbeat
from vonk_control.platform_observation import PlatformObserver
from vonk_control.worker import WorkerHeartbeatRecorder

from cluster_profiles import cli, runtime_identity
from cluster_profiles.control_client import ControlClient
from cluster_profiles.runtime_identity import RuntimeBuildIdentity


class Jobs:
    def enqueue(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("platform reads cannot enqueue")

    def get(self, job_id: str) -> object:
        raise KeyError(job_id)


class HttpResponse:
    def __init__(self, status: int, content: bytes) -> None:
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self._body = io.BytesIO(content)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self._body.close()

    def read(self, maximum: int) -> bytes:
        return self._body.read(maximum)


def test_platform_worker_fault_repair_through_authenticated_native_cli(
    tmp_path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'platform.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = [datetime(2026, 10, 7, tzinfo=UTC)]
    api_identity = RuntimeBuildIdentity("a" * 40, "c" * 64, "d" * 64)
    worker_identity = RuntimeBuildIdentity("b" * 40, "c" * 64, "e" * 64)
    monkeypatch.setattr(
        platform_observation, "packaged_runtime_identity", lambda: api_identity
    )
    monkeypatch.setattr(
        runtime_identity, "packaged_runtime_identity", lambda: worker_identity
    )
    recorder = WorkerHeartbeatRecorder(
        sessions, process_instance_id="f" * 64, clock=lambda: clock[0]
    )
    recorder.completed_loop()
    observer = PlatformObserver(sessions, clock=lambda: clock[0])
    codec = TokenCodec(b"k" * 32)
    token = codec.issue(Actor("viewer", "viewer"), ttl_seconds=100, now=0)
    app = create_app(
        jobs=Jobs(), tokens=codec, now=lambda: 10, platform_observer=observer
    )
    peer = TestClient(app)
    assert peer.get("/api/platform").status_code == 401
    token_path = tmp_path / "token"
    token_path.write_text(token)
    token_path.chmod(0o600)

    def opener(request, timeout):
        assert timeout <= 15
        response = peer.get("/api/platform", headers=dict(request.header_items()))
        return HttpResponse(response.status_code, response.content)

    client = ControlClient("https://control.invalid", token_path, opener=opener)
    monkeypatch.setattr(cli.ControlClient, "from_environment", lambda: client)

    def observe():
        output = io.StringIO()
        with redirect_stdout(output):
            assert cli.main(("--json", "platform")) == 0
        return json.loads(output.getvalue())

    healthy = observe()
    assert healthy["api"]["source_sha"] == "a" * 40
    assert healthy["workers"][0]["source_sha"] == "b" * 40
    assert healthy["workers"][0]["worker_contract_sha256"] == "e" * 64
    assert healthy["worker_issue"] is None
    clock[0] += timedelta(seconds=31)
    stale = observe()
    assert stale["api"] == healthy["api"]
    assert stale["workers"] is None
    assert stale["worker_issue"] == "worker-observation-unavailable"
    recorder.completed_loop()
    repaired = observe()
    assert repaired["workers"][0]["process_instance_id"] == "f" * 64
    assert repaired["workers"][0]["loop_sequence"] == 2
    assert repaired["workers"][0]["source_sha"] == "b" * 40
    with sessions() as session:
        row = session.scalar(select(ControlProcessHeartbeat))
        assert row is not None and row.source_sha == "b" * 40
    engine.dispose()
