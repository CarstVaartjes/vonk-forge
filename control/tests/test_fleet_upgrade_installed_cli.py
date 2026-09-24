"""Installed maintenance keeps one target in flight and stops on its failure."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol.package_source import AgentPackageSource
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_upgrades import AgentUpgradeService
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.fleet_projection import FleetProjection
from vonk_control.jobs import JobService
from vonk_control.models import AgentCertificate, AgentNode, AgentOperation, Base, Job
from vonk_control.operation_api import durable_operation_services
from vonk_control.operator_projection_api import FleetOperatorServices

from .test_agent_upgrades import NODE_A, NODE_B, OLD_IDENTITY, PACKAGE, REVISION, SOURCE
from .test_cli_first_connection_endpoints_installed import _Authority
from .test_profile_load_installed_cli import _https_api_peer, _process_environment

pytest_plugins = ("tests.test_profile_load_installed_cli",)


def test_installed_upgrade_reconnects_to_first_failure_without_dispatching_next_spark(
    installed_vonkctl: Path, postgres_engine, tmp_path: Path, monkeypatch
) -> None:
    now = datetime(2026, 8, 27, tzinfo=UTC)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                    architecture="linux-arm64",
                    semantic_version="0.1.0",
                    build_digest=OLD_IDENTITY["build_digest"],
                    binary_digest=OLD_IDENTITY["binary_digest"],
                    self_test_passed=True,
                    last_seen_at=now,
                )
            )
            session.flush()
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    not_before=now - timedelta(minutes=1),
                    not_after=now + timedelta(hours=8),
                    fingerprint=f"fingerprint-{serial}",
                )
            )
    monkeypatch.setattr(
        "vonk_control.agent_upgrades.load_package_source",
        lambda *_: AgentPackageSource.model_validate(SOURCE),
    )
    operations = AgentJobService(sessions, clock=lambda: now)
    upgrades = AgentUpgradeService(
        sessions, operations, clock=lambda: now, current_revision=lambda: REVISION
    )
    # The external release publisher is outside this control-plane acceptance;
    # admission, sequential dispatch and failure reconciliation are actual owners.
    monkeypatch.setattr(upgrades, "current_package", lambda: dict(PACKAGE))
    operations.set_result_consumer(upgrades.consume_agent_result)
    tokens = TokenCodec(b"installed-fleet-upgrade-test-key-32")
    projected = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: now,
        cursors=tokens.cursor_codec(),
        resume_agent_upgrade=upgrades.resume,
    )
    app = create_app(
        jobs=JobService(sessions, clock=lambda: now),
        tokens=tokens,
        audits=MemoryAuditStore(),
        fleet_projection=FleetProjection(_Authority(), sessions, clock=lambda: now),
        fleet_services=FleetOperatorServices(upgrades=upgrades),
        operations=projected,
        now=lambda: 100,
    )
    headers = {
        "Authorization": "Bearer "
        + tokens.issue(Actor("admin", "administrator"), ttl_seconds=200, now=0)
    }
    key = str(uuid4())
    with (
        TestClient(app) as api,
        _https_api_peer(tmp_path, api, headers) as (url, certificate, peer),
    ):
        environment = _process_environment(tmp_path, url, certificate, headers)

        def run(*args: str):
            return subprocess.run(
                [str(installed_vonkctl), *args],
                env=environment,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        refused = run("--no-input", "fleet", "upgrade", "--all", "--json")
        assert refused.returncode == 2, refused.stdout + refused.stderr
        assert "--yes" in json.loads(refused.stdout)["error"]
        assert not peer.calls
        with sessions() as session:
            assert not tuple(session.scalars(select(Job)))

        peer.drop_responses.add(("POST", "/api/fleet/upgrade"))
        accepted = run(
            "fleet",
            "upgrade",
            "--all",
            "--yes",
            "--strategy",
            "one-at-a-time",
            "--request-key",
            key,
            "--detach",
            "--json",
        )
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        receipt = json.loads(accepted.stdout)
        job_id = receipt["operation_id"]
        assert receipt["request_key"] == key
        assert receipt["targets"] == [NODE_A, NODE_B]
        first = operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        assert first is not None
        operations.fail(first, "agent upgrade request is invalid")
        observed = run("fleet", "progress", job_id, "--json")
        assert observed.returncode == 0, observed.stdout + observed.stderr
        result = json.loads(observed.stdout)
        assert result["id"] == job_id
        assert result["state"] == "waiting-for-operator"
        assert (
            operations.claim(
                NODE_B,
                "serial-b",
                30,
                capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                runtime_identity=OLD_IDENTITY,
            )
            is None
        )
        with sessions() as session:
            jobs = tuple(session.scalars(select(Job).where(Job.request_id == key)))
            children = tuple(
                session.scalars(
                    select(AgentOperation).where(AgentOperation.parent_job_id == job_id)
                )
            )
            assert len(jobs) == 1 and jobs[0].id == job_id
            assert len(children) == 1 and children[0].node_id == NODE_A
            assert children[0].state == "waiting-for-operator"
        assert peer.dropped_responses == [("POST", "/api/fleet/upgrade")]
