"""Installed Fleet removal consent through the registered PostgreSQL owner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor
from vonk_control.fleet_projection import FleetProjection
from vonk_control.models import AgentCertificate, AgentNode, AgentNodeProfile
from vonk_control.operator_projection_api import build_fleet_operator_services

from .test_agent_api import NODE_A, NODE_B, Jobs, make_agent_system
from .test_profile_load_installed_cli import _https_api_peer, _process_environment

pytest_plugins = ("tests.test_profile_load_installed_cli",)


class _RevisionSource:
    def head(self) -> str:
        return "a" * 64


def _run(
    executable: Path,
    arguments: tuple[str, ...],
    environment: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *arguments],
        env=environment,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


def _owner_state(
    sessions, node_id: str
) -> tuple[str, object, list[tuple[str, str, object]]]:
    with sessions() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        certificates = list(
            session.scalars(
                select(AgentCertificate)
                .where(AgentCertificate.node_id == node_id)
                .order_by(AgentCertificate.serial)
            )
        )
        return (
            node.state,
            node.revoked_at,
            [(value.serial, value.state, value.revoked_at) for value in certificates],
        )


@pytest.mark.lane
def test_installed_fleet_remove_requires_consent_and_uses_canonical_node(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """No-input refusals preserve enrollment; consent revokes only the resolved node."""

    _agent_api, agent_services, codec, clock = make_agent_system(
        tmp_path, engine=postgres_engine
    )
    sessions = agent_services.sessions
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
                agent_services=agent_services,
                upgrades=None,
            ),
        )
    )
    administrator = {
        "Authorization": "Bearer "
        + codec.issue(Actor("administrator", "administrator"), ttl_seconds=3_600, now=0)
    }
    remove_requests: list[tuple[object, object]] = []
    original_request = api.request

    def observe_remove_request(method, url, *args, **kwargs):
        if method.upper() == "POST" and url == f"/api/fleet/{NODE_A}/remove":
            remove_requests.append((kwargs.get("content"), kwargs.get("json")))
        return original_request(method, url, *args, **kwargs)

    monkeypatch.setattr(api, "request", observe_remove_request)
    before_atlas = _owner_state(sessions, NODE_A)
    before_borealis = _owner_state(sessions, NODE_B)
    assert before_atlas[0] == before_borealis[0] == "active"
    assert before_atlas[1] is before_borealis[1] is None

    with _https_api_peer(tmp_path, api, administrator) as (url, certificate, peer):
        environment = _process_environment(tmp_path, url, certificate, administrator)
        for consent_mode in ("--json", "--no-input"):
            refused = _run(
                installed_vonkctl,
                (consent_mode, "fleet", "remove", "Atlas"),
                environment,
                tmp_path,
            )
            assert refused.returncode == 2, refused.stdout + refused.stderr
            assert "--yes" in refused.stdout + refused.stderr
            assert not [
                (method, path)
                for method, path, _document in peer.calls
                if method == "POST" and path.endswith("/remove")
            ]
            assert _owner_state(sessions, NODE_A) == before_atlas
            assert _owner_state(sessions, NODE_B) == before_borealis

        accepted = _run(
            installed_vonkctl,
            ("--json", "fleet", "remove", "Atlas", "--yes"),
            environment,
            tmp_path,
        )
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        receipt = json.loads(accepted.stdout)
        assert receipt["action"] == "remove"
        assert receipt["node_id"] == NODE_A
        assert receipt["state"] == "accepted"

        remove_calls = [
            (method, path, document)
            for method, path, document in peer.calls
            if method == "POST" and path.endswith("/remove")
        ]
        assert remove_calls == [("POST", f"/api/fleet/{NODE_A}/remove", None)]
        assert remove_requests == [(None, None)]

    after_atlas = _owner_state(sessions, NODE_A)
    after_borealis = _owner_state(sessions, NODE_B)
    assert after_atlas[0] == "retired" and after_atlas[1] is not None
    assert len(after_atlas[2]) == 1
    assert after_atlas[2][0][1] == "revoked"
    assert after_atlas[2][0][2] is not None
    assert after_borealis == before_borealis
