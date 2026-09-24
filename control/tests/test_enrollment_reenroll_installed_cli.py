"""Installed re-enrollment reviews the exact Spark before issuing a grant."""

from __future__ import annotations

import json
import os
import pty
import select as select_io
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor
from vonk_control.fleet_projection import FleetProjection
from vonk_control.models import AgentEnrollmentGrant, AgentNodeProfile
from vonk_control.operator_projection_api import build_fleet_operator_services

from .test_agent_api import NODE_A, NODE_B, Jobs, make_agent_system
from .test_profile_load_installed_cli import (
    _https_api_peer,
    _process_environment,
    _run_pty,
)

pytest_plugins = ("tests.test_profile_load_installed_cli",)


class _RevisionSource:
    def head(self) -> str:
        return "a" * 64


def _stack(postgres_engine, tmp_path: Path):
    _agent_api, services, codec, clock = make_agent_system(
        tmp_path, engine=postgres_engine
    )
    sessions = services.sessions
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNodeProfile(
                    node_id=NODE_A,
                    display_name="Atlas",
                    hostname="atlas.example.test",
                ),
                AgentNodeProfile(
                    node_id=NODE_B,
                    display_name="Borealis",
                    hostname="borealis.example.test",
                ),
            ]
        )
    api = TestClient(
        create_app(
            jobs=Jobs(),
            tokens=codec,
            audits=MemoryAuditStore(),
            now=lambda: 0,
            fleet_projection=FleetProjection(_RevisionSource(), sessions, clock=clock),
            fleet_services=build_fleet_operator_services(
                agent_services=services, upgrades=None
            ),
        )
    )
    headers = {
        "Authorization": "Bearer "
        + codec.issue(Actor("maintainer", "administrator"), ttl_seconds=3600, now=0)
    }
    return api, services, headers


def _review_text() -> str:
    return (
        f"Authorize a replacement certificate for Atlas ({NODE_A})? "
        "The one-time grant permits replacing this Spark identity when consumed."
    )


def _confirm_after_review_is_visible(
    executable: Path,
    arguments: tuple[str, ...],
    environment: dict[str, str],
    cwd: Path,
    before_confirm: Callable[[], None],
) -> tuple[int, str, str]:
    """Observe the review before allowing an installed process to confirm it."""

    master, slave = pty.openpty()
    process = subprocess.Popen(
        [str(executable), *arguments],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=slave,
        env=environment,
        cwd=cwd,
    )
    os.close(slave)
    transcript = bytearray()
    answer_sent = False
    deadline = time.monotonic() + 45
    try:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("installed CLI did not reach its review prompt")
            ready, _, _ = select_io.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65_536)
                except OSError:
                    chunk = b""
                transcript.extend(chunk)
                if not answer_sent and b"[y/N]" in transcript:
                    observed_prompt = transcript.decode("utf-8", errors="replace")
                    assert _review_text() in observed_prompt
                    before_confirm()
                    os.write(master, b"yes\n")
                    answer_sent = True
            if answer_sent and process.poll() is not None:
                break
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        raise
    finally:
        os.close(master)

    stdout, _ = process.communicate(timeout=5)
    assert answer_sent
    assert process.returncode is not None
    return (
        process.returncode,
        stdout.decode("utf-8", errors="replace"),
        transcript.decode("utf-8", errors="replace"),
    )


@pytest.mark.lane
@pytest.mark.parametrize("mode", ["decline", "eof", "no-input"])
def test_installed_reenroll_requires_review_before_mutating_the_owner(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
    mode: str,
) -> None:
    """The installed prompt names the exact node and refusal leaves it alone."""

    api, services, headers = _stack(postgres_engine, tmp_path)
    key = str(uuid4())
    destination = tmp_path / f"{mode}-replacement-grant.json"
    arguments = (
        "fleet",
        "re-enroll",
        "Atlas",
        "--output",
        str(destination),
        "--request-key",
        key,
    )

    with _https_api_peer(tmp_path, api, headers) as (url, certificate, peer):
        environment = _process_environment(tmp_path, url, certificate, headers)
        if mode == "no-input":
            refused = subprocess.run(
                [
                    str(installed_vonkctl),
                    "--no-input",
                    *arguments,
                    "--json",
                ],
                env=environment,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            assert refused.returncode == 2, refused.stdout + refused.stderr
            result = json.loads(refused.stdout)
            assert _review_text() in result["error"]
            assert "Pass --yes" in result["error"]
            assert not refused.stderr
        else:
            status, stdout, stderr = _run_pty(
                installed_vonkctl,
                arguments,
                environment,
                tmp_path,
                answer="no" if mode == "decline" else None,
            )
            assert status == 2, stdout + stderr
            assert not stdout
            assert _review_text() in stderr
            assert stderr.count("[y/N]") == 1
            if mode == "eof":
                assert "action was not confirmed" in stderr

        assert peer.calls == [("GET", "/api/fleet/Atlas", None)]
        assert not destination.exists()
        with services.sessions() as session:
            grants = list(
                session.scalars(
                    select(AgentEnrollmentGrant).where(
                        AgentEnrollmentGrant.purpose == "re-enroll"
                    )
                )
            )
        assert grants == []


@pytest.mark.lane
def test_installed_reenroll_confirmation_targets_exact_node_and_keeps_grant_private(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
) -> None:
    api, services, headers = _stack(postgres_engine, tmp_path)
    key = str(uuid4())
    destination = tmp_path / "confirmed-replacement-grant.json"
    arguments = (
        "fleet",
        "re-enroll",
        "Atlas",
        "--output",
        str(destination),
        "--request-key",
        key,
    )

    with _https_api_peer(tmp_path, api, headers) as (url, certificate, peer):
        environment = _process_environment(tmp_path, url, certificate, headers)

        def verify_not_submitted_before_confirmation() -> None:
            assert peer.calls == [("GET", "/api/fleet/Atlas", None)]
            with services.sessions() as session:
                grant_id = session.scalar(
                    select(AgentEnrollmentGrant.id).where(
                        AgentEnrollmentGrant.purpose == "re-enroll"
                    )
                )
            assert grant_id is None

        status, stdout, stderr = _confirm_after_review_is_visible(
            installed_vonkctl,
            arguments,
            environment,
            tmp_path,
            verify_not_submitted_before_confirmation,
        )

    assert status == 0, stdout + stderr
    assert _review_text() in stderr
    assert stderr.count("[y/N]") == 1
    grant = json.loads(destination.read_text())
    token = grant["token"]
    assert grant["id"] == key
    assert grant["purpose"] == "re-enroll"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert token not in stdout + stderr
    assert str(destination) in stdout
    assert peer.calls == [
        ("GET", "/api/fleet/Atlas", None),
        (
            "POST",
            f"/api/fleet/{NODE_A}/re-enroll",
            {"request_key": key},
        ),
    ]
    with services.sessions() as session:
        persisted = session.get(AgentEnrollmentGrant, key)
        assert persisted is not None
        assert persisted.node_id == NODE_A
        assert persisted.purpose == "re-enroll"
        assert persisted.created_by == "maintainer"
        assert persisted.consumed_at is None
        assert persisted.revoked_at is None
