"""Installed dependency-free CLI observes actual API and worker authority."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control import platform_observation
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.models import Base
from vonk_control.platform_observation import PlatformObserver
from vonk_control.worker import WorkerHeartbeatRecorder

from cluster_profiles import runtime_identity
from cluster_profiles.runtime_identity import RuntimeBuildIdentity
from tests.test_platform_observation import Jobs
from tests.test_profile_load_installed_cli import _https_api_peer, _process_environment

pytest_plugins = ("tests.test_profile_load_installed_cli",)


@pytest.mark.lane
def test_installed_platform_preserves_separate_sources_and_worker_recovery(
    installed_vonkctl: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    codec = TokenCodec(b"k" * 32)
    token = codec.issue(Actor("viewer", "viewer"), ttl_seconds=100, now=0)
    peer = TestClient(
        create_app(
            jobs=Jobs(),
            tokens=codec,
            now=lambda: 10,
            platform_observer=PlatformObserver(sessions, clock=lambda: clock[0]),
        )
    )
    with _https_api_peer(tmp_path, peer, {"Authorization": f"Bearer {token}"}) as (
        url,
        certificate,
        _state,
    ):
        environment = _process_environment(
            tmp_path, url, certificate, {"Authorization": f"Bearer {token}"}
        )

        def command(*arguments: str):
            result = subprocess.run(
                [str(installed_vonkctl), "--json", *arguments],
                env=environment,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        client = command("--version")
        assert client["source_sha"] == "d" * 40
        assert isinstance(client["control_contract_sha256"], str)
        assert len(client["control_contract_sha256"]) == 64
        healthy = command("platform")
        assert healthy["api"]["source_sha"] == "a" * 40
        assert healthy["workers"][0]["source_sha"] == "b" * 40
        clock[0] += timedelta(seconds=31)
        stale = command("platform")
        assert stale["api"] == healthy["api"]
        assert stale["workers"] is None
        assert stale["worker_issue"] == "worker-observation-unavailable"
        recorder.completed_loop()
        repaired = command("platform")
        assert repaired["workers"][0]["loop_sequence"] == 2
        assert repaired["workers"][0]["source_sha"] == "b" * 40
    engine.dispose()
