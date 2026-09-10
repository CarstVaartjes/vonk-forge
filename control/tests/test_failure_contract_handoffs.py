"""Connected durable failure -> family API -> Activity contract checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentResult
from vonk_agent_protocol.contracts import AgentFailureResult
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperationAttempt,
    Base,
    Job,
)
from vonk_control.operation_api import durable_operation_services
from vonk_control.strict_json import serialize_json_value

from .runtime_identity_support import claim_agent
from .test_agent_jobs import COMMIT, NODE_A, STOP_PAYLOAD, Clock, parent
from .test_operation_api import Jobs

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)


@pytest.fixture
def sessions(postgres_engine):
    Base.metadata.create_all(postgres_engine)
    return sessionmaker(postgres_engine, expire_on_commit=False)


def client_for(sessions, tmp_path):
    tokens = TokenCodec(b"f" * 32)
    operations = durable_operation_services(
        sessions, tmp_path / "routes", clock=lambda: NOW, cursors=tokens.cursor_codec()
    )
    app = create_app(
        jobs=Jobs(),
        tokens=tokens,
        audits=MemoryAuditStore(),
        now=lambda: 0,
        operations=operations,
    )
    token = tokens.issue(Actor("admin", "administrator"), ttl_seconds=100, now=0)
    return TestClient(app), {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "failure",
    [
        {"reason": "The image could not be acquired"},
        {
            "error_code": "failure." + "x" * 120,
            "summary": "s" * 1024,
            "uncertain": True,
        },
    ],
)
def test_persisted_agent_failure_survives_activity(sessions, tmp_path, failure):
    clock = Clock()
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                capabilities=[],
                architecture="linux-arm64",
                semantic_version="1.0.0",
                build_digest="sha256:" + "f" * 64,
                binary_digest="f" * 64,
                self_test_passed=True,
            )
        )
        session.flush()
        session.add(
            AgentCertificate(
                serial="serial-a",
                node_id=NODE_A,
                not_before=clock.now - timedelta(seconds=1),
                not_after=clock.now + timedelta(hours=1),
                fingerprint="fingerprint-a",
            )
        )
    jobs = AgentJobService(sessions, clock=clock)
    job = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, job.id).targets = [NODE_A]
    operation = jobs.enqueue(job.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    claim = claim_agent(jobs, NODE_A, "serial-a", 30)
    message = AgentResult(
        **{
            key: getattr(claim, key)
            for key in (
                "schema_version",
                "job_id",
                "operation_id",
                "attempt",
                "fence",
                "node_id",
                "deadline",
            )
        },
        state="failed",
        result=AgentFailureResult.model_validate(failure).model_dump(
            mode="json", exclude_none=True
        ),
    )
    jobs.record_result(message)
    with sessions() as session:
        persisted = (
            session.query(AgentOperationAttempt)
            .filter_by(operation_id=operation.id)
            .one()
            .result
        )
    client, headers = client_for(sessions, tmp_path)
    response = client.get(f"/api/operations/{operation.id}", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["failure"] == serialize_json_value(
        AgentFailureResult.model_validate(persisted)
    )
    listing = client.get("/api/operations", headers=headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["operations"][0]["failure"] == response.json()["failure"]
