"""Installed fleet resume rechecks the caller at the registered job route."""

from __future__ import annotations

import copy
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
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
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
)
from vonk_control.operation_api import durable_operation_services
from vonk_control.operator_projection_api import FleetOperatorServices

from .test_agent_upgrades import NODE_A, NODE_B, OLD_IDENTITY, PACKAGE, REVISION, SOURCE
from .test_cli_first_connection_endpoints_installed import _Authority
from .test_profile_load_installed_cli import _https_api_peer, _process_environment

pytest_plugins = ("tests.test_profile_load_installed_cli",)


@pytest.mark.lane
def test_installed_fleet_resume_rechecks_role_and_preserves_exact_job_attempt(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    clock_value = [datetime(2026, 9, 24, tzinfo=UTC)]
    clock = lambda: clock_value[0]
    now = clock()
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
    operations = AgentJobService(sessions, clock=clock)
    upgrades = AgentUpgradeService(
        sessions, operations, clock=clock, current_revision=lambda: REVISION
    )
    # Package publication is outside this route and installed-CLI acceptance.
    monkeypatch.setattr(upgrades, "current_package", lambda: dict(PACKAGE))
    operations.set_result_consumer(upgrades.consume_agent_result)
    tokens = TokenCodec(b"installed-fleet-resume-test-key-32")
    projected = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=clock,
        cursors=tokens.cursor_codec(),
        resume_agent_upgrade=upgrades.resume,
    )
    app = create_app(
        jobs=JobService(sessions, clock=clock),
        tokens=tokens,
        audits=MemoryAuditStore(),
        fleet_projection=FleetProjection(_Authority(), sessions, clock=clock),
        fleet_services=FleetOperatorServices(upgrades=upgrades),
        operations=projected,
        now=lambda: 100,
    )
    administrator = "Bearer " + tokens.issue(
        Actor("maintainer", "administrator"), ttl_seconds=1_000, now=0
    )
    viewer = "Bearer " + tokens.issue(
        Actor("maintainer", "viewer"), ttl_seconds=1_000, now=0
    )
    headers = {"Authorization": administrator}
    request_key = str(uuid4())

    with (
        TestClient(app) as api,
        _https_api_peer(tmp_path, api, headers) as (url, certificate, peer),
    ):
        environment = _process_environment(tmp_path, url, certificate, headers)

        def run(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(installed_vonkctl), *arguments],
                env=environment,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )

        submitted = run(
            "fleet",
            "upgrade",
            "--all",
            "--yes",
            "--strategy",
            "one-at-a-time",
            "--request-key",
            request_key,
            "--detach",
            "--json",
        )
        assert submitted.returncode == 0, submitted.stdout + submitted.stderr
        receipt = json.loads(submitted.stdout)
        job_id = receipt["operation_id"]
        assert receipt["request_key"] == request_key
        assert receipt["targets"] == [NODE_A, NODE_B]

        first_attempt = operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        assert first_attempt is not None
        operations.fail(first_attempt, "agent upgrade request is invalid")
        # The first helper refusal owns one automatic retry behind the full
        # install recovery fence. Only the subsequent parked failure is an
        # operator-resume decision.
        clock_value[0] += timedelta(seconds=961)
        child_attempt = operations.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=OLD_IDENTITY,
        )
        assert child_attempt is not None
        assert child_attempt.operation_id == first_attempt.operation_id
        assert child_attempt.attempt == 2
        operations.fail(child_attempt, "agent upgrade request is invalid")
        clock_value[0] += timedelta(minutes=2)

        with sessions() as session:
            job = session.get(Job, job_id)
            child = session.scalar(
                select(AgentOperation).where(AgentOperation.parent_job_id == job_id)
            )
            assert job is not None and child is not None
            assert job.state == "waiting-for-operator"
            assert child.state == "waiting-for-operator"
            assert child.id == child_attempt.operation_id
            original_identity = (
                job.id,
                job.request_id,
                job.authority_revision,
                job.payload_digest,
                copy.deepcopy(job.payload),
                list(job.targets),
                job.current_attempt,
                child.id,
                child.authority_revision,
                child.payload_digest,
                copy.deepcopy(child.payload),
                child.current_attempt,
            )
            original_attempt_audit = tuple(
                session.execute(
                    select(
                        AgentOperationAttempt.id,
                        AgentOperationAttempt.attempt,
                        AgentOperationAttempt.fence,
                        AgentOperationAttempt.state,
                        AgentOperationAttempt.result,
                        AgentOperationAttempt.lease_deadline,
                    )
                    .where(AgentOperationAttempt.operation_id == child.id)
                    .order_by(AgentOperationAttempt.attempt)
                )
            )
            assert [(row[1], row[3]) for row in original_attempt_audit] == [
                (1, "failed"),
                (2, "failed"),
            ]

        # The owner advertises the action on a real GET. Change the signed caller
        # role only at the POST boundary to model authority being revoked between
        # review and submission; the registered route must deny dispatch.
        original_request = api.request
        downgraded = False
        resume_responses: list[tuple[str, str, int, object]] = []

        def request(method: str, path: str, **kwargs):
            nonlocal downgraded
            outgoing_headers = dict(kwargs.get("headers") or {})
            if (
                method == "POST"
                and path == f"/api/jobs/{job_id}/resume"
                and not downgraded
            ):
                assert outgoing_headers.get("Authorization") == administrator
                outgoing_headers["Authorization"] = viewer
                kwargs["headers"] = outgoing_headers
                downgraded = True
            response = original_request(method, path, **kwargs)
            if method == "GET" and path == f"/api/jobs/{job_id}":
                document = response.json()
                assert document["id"] == job_id
                assert document["state"] == "waiting-for-operator"
                assert "resume" in document["recovery"]["actions"]
            if method == "POST" and path == f"/api/jobs/{job_id}/resume":
                resume_responses.append(
                    (method, path, response.status_code, response.json())
                )
            return response

        monkeypatch.setattr(api, "request", request)
        denied = run("fleet", "resume", job_id, "--yes", "--json")
        assert downgraded
        assert denied.returncode == 2, denied.stdout + denied.stderr
        assert resume_responses == [
            (
                "POST",
                f"/api/jobs/{job_id}/resume",
                403,
                {"detail": "insufficient role"},
            )
        ]
        resume_calls = [
            call
            for call in peer.calls
            if call[1] in {f"/api/jobs/{job_id}", f"/api/jobs/{job_id}/resume"}
        ]
        assert [(method, path, body) for method, path, body in resume_calls] == [
            ("GET", f"/api/jobs/{job_id}", None),
            (
                "POST",
                f"/api/jobs/{job_id}/resume",
                {"disposition": "resume"},
            ),
        ]
        with sessions() as session:
            job = session.get(Job, job_id)
            child = session.get(AgentOperation, child_attempt.operation_id)
            assert job is not None and child is not None
            after_denial = (
                job.id,
                job.request_id,
                job.authority_revision,
                job.payload_digest,
                copy.deepcopy(job.payload),
                list(job.targets),
                job.current_attempt,
                child.id,
                child.authority_revision,
                child.payload_digest,
                copy.deepcopy(child.payload),
                child.current_attempt,
            )
            denied_attempt_audit = tuple(
                session.execute(
                    select(
                        AgentOperationAttempt.id,
                        AgentOperationAttempt.attempt,
                        AgentOperationAttempt.fence,
                        AgentOperationAttempt.state,
                        AgentOperationAttempt.result,
                        AgentOperationAttempt.lease_deadline,
                    )
                    .where(AgentOperationAttempt.operation_id == child.id)
                    .order_by(AgentOperationAttempt.attempt)
                )
            )
            assert job.state == "waiting-for-operator"
            assert child.state == "waiting-for-operator"
            assert after_denial == original_identity
            assert denied_attempt_audit == original_attempt_audit

        assert (
            operations.claim(
                NODE_A,
                "serial-a",
                30,
                capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                runtime_identity=OLD_IDENTITY,
            )
            is None
        )

        second_resume_start = len(peer.calls)
        resumed = run("fleet", "resume", job_id, "--yes", "--json")
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        resume_receipt = json.loads(resumed.stdout)
        assert resume_receipt["id"] == job_id
        assert resume_receipt["state"] == "queued"
        assert resume_responses[-1] == (
            "POST",
            f"/api/jobs/{job_id}/resume",
            202,
            {"id": job_id, "state": "queued"},
        )
        new_resume_calls = [
            call
            for call in peer.calls[second_resume_start:]
            if call[1] in {f"/api/jobs/{job_id}", f"/api/jobs/{job_id}/resume"}
        ]
        assert [(method, path, body) for method, path, body in new_resume_calls] == [
            ("GET", f"/api/jobs/{job_id}", None),
            (
                "POST",
                f"/api/jobs/{job_id}/resume",
                {"disposition": "resume"},
            ),
        ]

        with sessions() as session:
            job = session.get(Job, job_id)
            child = session.get(AgentOperation, child_attempt.operation_id)
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == child_attempt.operation_id,
                    AgentOperationAttempt.attempt == child_attempt.attempt,
                )
            )
            children = tuple(
                session.scalars(
                    select(AgentOperation).where(AgentOperation.parent_job_id == job_id)
                )
            )
            assert job is not None and child is not None and attempt is not None
            assert job.state == "queued"
            assert child.state == "waiting-for-operator"
            assert child.retry_disposition == "retry"
            assert child.retry_disposition_attempt == child_attempt.attempt
            assert job.request_id == request_key
            assert len(children) == 1 and children[0].id == child_attempt.operation_id
            assert (
                job.id,
                job.request_id,
                job.authority_revision,
                job.payload_digest,
                copy.deepcopy(job.payload),
                list(job.targets),
                job.current_attempt,
                child.id,
                child.authority_revision,
                child.payload_digest,
                copy.deepcopy(child.payload),
                child.current_attempt,
            ) == original_identity
            attempt_audit = tuple(
                session.execute(
                    select(
                        AgentOperationAttempt.id,
                        AgentOperationAttempt.attempt,
                        AgentOperationAttempt.fence,
                        AgentOperationAttempt.state,
                        AgentOperationAttempt.result,
                    )
                    .where(AgentOperationAttempt.operation_id == child.id)
                    .order_by(AgentOperationAttempt.attempt)
                )
            )
            original_audit_without_deadline = tuple(
                row[:5] for row in original_attempt_audit
            )
            assert attempt_audit == original_audit_without_deadline
            deadline = attempt.lease_deadline
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            assert deadline == clock() + timedelta(seconds=960)

        assert (
            operations.claim(
                NODE_A,
                "serial-a",
                30,
                capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                runtime_identity=OLD_IDENTITY,
            )
            is None
        )
