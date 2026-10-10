"""Hosted installed consumer proves complete large Fleet/platform observations."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.models import Base, ControlProcessHeartbeat
from vonk_control.platform_observation import PlatformObservation, PlatformObserver
from vonk_control.strict_json import serialize_json_value

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES
from tests.observation_transfer_peer import fleet_authority
from tests.test_observation_transfer import NOW, Projection, _large_snapshot
from tests.test_platform_observation import Jobs
from tests.test_profile_load_installed_cli import _https_api_peer, _process_environment

pytest_plugins = ("tests.test_profile_load_installed_cli",)


@pytest.mark.lane
def test_installed_cli_receives_complete_large_fleet_and_mixed_worker_membership(
    installed_vonkctl: Path,
    tmp_path: Path,
) -> None:
    snapshot = _large_snapshot(tmp_path)
    snapshot.nodes.append(
        snapshot.nodes[0].model_copy(update={"id": "spk_" + "2" * 32})
    )
    projection = Projection(snapshot)
    engine = create_engine(f"sqlite:///{tmp_path / 'workers.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add_all(
            [
                ControlProcessHeartbeat(
                    process_kind="worker",
                    process_instance_id=f"{index:064x}",
                    source_sha=("a" if index % 2 == 0 else "b") * 40,
                    worker_contract_sha256=("c" if index % 2 == 0 else "d") * 64,
                    loop_sequence=1,
                    completed_at=NOW,
                )
                for index in range(5000)
            ]
        )
    observer = PlatformObserver(sessions, clock=lambda: NOW)
    platform = observer.read()
    assert (
        len(json.dumps(serialize_json_value(platform)).encode())
        > MAX_CONTROL_DOCUMENT_BYTES
    )
    codec = TokenCodec(b"k" * 32)
    token = codec.issue(Actor("viewer", "viewer"), ttl_seconds=100, now=0)
    peer = TestClient(
        create_app(
            jobs=Jobs(),
            tokens=codec,
            now=lambda: 10,
            fleet_projection=projection,
            platform_observer=observer,
        )
    )
    headers = {"Authorization": f"Bearer {token}"}
    with _https_api_peer(tmp_path, peer, headers) as (url, certificate, _state):
        environment = _process_environment(tmp_path, url, certificate, headers)

        def command(noun: str) -> str:
            result = subprocess.run(
                [str(installed_vonkctl), "--json", noun],
                env=environment,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            return result.stdout

        fleet = fleet_authority(json.loads(command("fleet")))
        assert fleet == snapshot
        assert len(fleet.nodes) == 2
        workers = PlatformObservation.model_validate_json(
            command("platform"), strict=True
        )
        assert workers == platform
        members = workers.workers
        assert members is not None
        assert len(members) == 5000
        assert {worker.source_sha for worker in members} == {
            "a" * 40,
            "b" * 40,
        }
        assert {worker.worker_contract_sha256 for worker in members} == {
            "c" * 64,
            "d" * 64,
        }
        # A subsequent read captures new desired membership instead of mixing
        # its bytes into the complete prior observation.
        snapshot.nodes.pop()
        snapshot.event_cursor += 1
        assert fleet_authority(json.loads(command("fleet"))) == snapshot
    engine.dispose()
