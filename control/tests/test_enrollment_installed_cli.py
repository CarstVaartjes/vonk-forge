"""Private enrollment delivery and lost-answer recovery through the installed CLI."""

from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor
from vonk_control.models import AgentEnrollmentGrant
from vonk_control.operator_projection_api import build_fleet_operator_services

from .test_agent_api import Jobs, make_agent_system
from .test_profile_load_installed_cli import _https_api_peer, _process_environment

pytest_plugins = ("tests.test_profile_load_installed_cli",)


@pytest.mark.parametrize("lose_answer", [False, True])
def test_installed_enrollment_keeps_private_grant_and_original_identity(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
    monkeypatch,
    lose_answer: bool,
) -> None:
    _agent_api, services, codec, _clock = make_agent_system(
        tmp_path, engine=postgres_engine
    )
    api = TestClient(
        create_app(
            jobs=Jobs(),
            tokens=codec,
            audits=MemoryAuditStore(),
            now=lambda: 0,
            fleet_services=build_fleet_operator_services(
                agent_services=services, upgrades=None
            ),
        )
    )
    headers = {
        "Authorization": "Bearer "
        + codec.issue(Actor("administrator", "administrator"), ttl_seconds=3600, now=0)
    }
    key = str(uuid4())
    destination = tmp_path / "private-grant.json"
    secrets: list[str] = []
    original_request = api.request

    def record_committed_response(method, path, *args, **kwargs):
        response = original_request(method, path, *args, **kwargs)
        if method == "POST" and path == "/api/fleet/enroll":
            assert response.status_code == 201, response.text
            secrets.append(response.json()["grant"]["token"])
        return response

    monkeypatch.setattr(api, "request", record_committed_response)
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, peer):
        env = _process_environment(tmp_path, url, certificate, headers)
        if lose_answer:
            peer.drop_responses.add(("POST", "/api/fleet/enroll"))

        def run(*arguments):
            return subprocess.run(
                [str(installed_vonkctl), *arguments, "--json"],
                env=env,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )

        issued = run(
            "fleet",
            "enroll",
            "Private Spark",
            "--output",
            str(destination),
            "--request-key",
            key,
        )
        assert issued.returncode == (2 if lose_answer else 0), (
            issued.stdout + issued.stderr
        )
        assert len(secrets) == 1
        assert secrets[0] not in issued.stdout + issued.stderr
        assert destination.stat().st_mode & 0o777 == 0o600
        grant = json.loads(destination.read_text())
        assert grant["id"] == key
        if lose_answer:
            assert "token" not in grant
            assert grant["grant_status"]["state"] == "pending"
        else:
            assert grant["token"] == secrets[0]
        with services.sessions() as session:
            rows = list(session.scalars(select(AgentEnrollmentGrant)))
            assert len(rows) == 1 and rows[0].id == key

        # A new installed process observes and revokes the exact issued grant.
        status = run("fleet", "enrollment", "status", key)
        assert status.returncode == 0, status.stdout + status.stderr
        assert json.loads(status.stdout)["state"] == "pending"
        revoked = run("fleet", "enrollment", "revoke", key, "--yes")
        assert revoked.returncode == 0, revoked.stdout + revoked.stderr
        assert json.loads(revoked.stdout)["state"] == "revoked"
        assert (
            secrets[0]
            not in status.stdout + status.stderr + revoked.stdout + revoked.stderr
        )
        assert (
            sum(
                method == "POST" and path == "/api/fleet/enroll"
                for method, path, _ in peer.calls
            )
            == 1
        )


@pytest.mark.lane
def test_installed_enrollment_process_death_recovers_original_grant_identity(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A killed client can inspect accepted work without minting another grant."""

    _agent_api, services, codec, _clock = make_agent_system(
        tmp_path, engine=postgres_engine
    )
    api = TestClient(
        create_app(
            jobs=Jobs(),
            tokens=codec,
            audits=MemoryAuditStore(),
            now=lambda: 0,
            fleet_services=build_fleet_operator_services(
                agent_services=services, upgrades=None
            ),
        )
    )
    headers = {
        "Authorization": "Bearer "
        + codec.issue(Actor("administrator", "administrator"), ttl_seconds=3600, now=0)
    }
    key = str(uuid4())
    destination = tmp_path / "process-death-grant.json"
    secrets: list[str] = []
    original_request = api.request

    def record_accepted_secret(method, path, *args, **kwargs):
        response = original_request(method, path, *args, **kwargs)
        if method == "POST" and path == "/api/fleet/enroll":
            assert response.status_code == 201, response.text
            grant = response.json()["grant"]
            assert grant["id"] == key
            secrets.append(grant["token"])
        return response

    monkeypatch.setattr(api, "request", record_accepted_secret)
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, peer):
        peer.held_response = ("POST", "/api/fleet/enroll")
        environment = _process_environment(tmp_path, url, certificate, headers)
        process = subprocess.Popen(
            [
                str(installed_vonkctl),
                "fleet",
                "enroll",
                "Process-death Spark",
                "--output",
                str(destination),
                "--request-key",
                key,
                "--json",
            ],
            env=environment,
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert peer.response_accepted.wait(timeout=30), (
                "the registered enrollment route did not accept the grant"
            )
            assert process.poll() is None, "the CLI exited before the held response"
            assert len(secrets) == 1
            secret = secrets[0]

            with services.sessions() as session:
                grants = list(session.scalars(select(AgentEnrollmentGrant)))
            assert len(grants) == 1
            assert grants[0].id == key
            assert grants[0].requested_display_name == "Process-death Spark"
            assert grants[0].consumed_at is None and grants[0].revoked_at is None

            # The intent receipt was fsynced before POST. Killing the actual
            # installed process here prevents it from receiving or writing the
            # one-time credential after PostgreSQL has durably accepted it.
            receipt = json.loads(destination.read_text())
            assert destination.stat().st_mode & 0o777 == 0o600
            assert receipt["id"] == key
            assert receipt["delivery"] == {"status": "pending"}
            assert "token" not in receipt
            process.kill()
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == -signal.SIGKILL
            assert secret not in stdout + stderr + destination.read_text()

            peer.discard_held_response = True
            peer.release_held_response.set()
            recovered = subprocess.run(
                [
                    str(installed_vonkctl),
                    "fleet",
                    "enrollment",
                    "status",
                    key,
                    "--json",
                ],
                env=environment,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert recovered.returncode == 0, recovered.stdout + recovered.stderr
            status = json.loads(recovered.stdout)
            assert status["id"] == key and status["state"] == "pending"
            assert secret not in recovered.stdout + recovered.stderr
            assert [
                (method, path)
                for method, path, _ in peer.calls
                if (method, path)
                in {
                    ("POST", "/api/fleet/enroll"),
                    ("GET", f"/api/fleet/enrollments/{key}"),
                }
            ] == [
                ("POST", "/api/fleet/enroll"),
                ("GET", f"/api/fleet/enrollments/{key}"),
            ]
        finally:
            peer.discard_held_response = True
            peer.release_held_response.set()
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
