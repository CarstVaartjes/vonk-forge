"""Stored failure decoration cannot block unrelated durable operation reads."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
)
from vonk_control.operation_api import (
    durable_operation_services,
    operation_detail_response,
)

from .test_operation_api import COMMIT, DIGEST, NODE_ID, _client


@pytest.mark.usefixtures("damaged_json_rows")
def test_oversized_stored_result_retains_identity_and_unrelated_operations(tmp_path):
    now = datetime(2026, 8, 5, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'result-isolation.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    damaged_id = "11111111-1111-4111-8111-111111111111"
    readable_id = "22222222-2222-4222-8222-222222222222"
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_ID, state="active"))
        job = Job(
            request_id="33333333-3333-4333-8333-333333333333",
            kind="reconcile",
            state="running",
            actor="operator",
            authority_revision=COMMIT,
            targets=[NODE_ID],
            payload_digest=DIGEST,
            payload={},
            current_attempt=1,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        for identity in (damaged_id, readable_id):
            session.add(
                AgentOperation(
                    id=identity,
                    parent_job_id=job.id,
                    node_id=NODE_ID,
                    kind="runtime.preflight.v1",
                    payload_digest=DIGEST,
                    payload={},
                    authority_revision=COMMIT,
                    state="failed" if identity == damaged_id else "running",
                    current_attempt=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.add(
            AgentOperationAttempt(
                operation_id=damaged_id,
                attempt=1,
                fence="44444444-4444-4444-8444-444444444444",
                lease_deadline=now + timedelta(seconds=60),
                agent_certificate_serial="certificate",
                state="failed",
                result={"reason": "Known failure"},
            )
        )
    services = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: now,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )
    client, operator, *_ = _client(operations=services)
    before = client.get(f"/api/operations/{damaged_id}", headers=operator)
    assert before.status_code == 200
    with sessions.begin() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == damaged_id
            )
        )
        assert attempt is not None
        attempt.result = {"reason": "x" * 4097}
    page = client.get("/api/operations", headers=operator)
    assert page.status_code == 200
    items = {item["id"]: item for item in page.json()["operations"]}
    assert items[readable_id]["state"] == "running"
    assert services.get_operation is not None
    operation_detail_response(services.get_operation(damaged_id))
    damaged = client.get(f"/api/operations/{damaged_id}", headers=operator)
    assert damaged.status_code == 200 and damaged.json()["id"] == damaged_id
    assert damaged.json()["failure"]["uncertain"] is True
    assert items[damaged_id]["id"] == damaged_id
    assert (
        client.get(f"/api/operations/{readable_id}", headers=operator).status_code
        == 200
    )
    with sessions.begin() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == damaged_id
            )
        )
        assert attempt is not None and attempt.result == {"reason": "x" * 4097}
        attempt.result = {"reason": "Known failure"}
    repaired = client.get(f"/api/operations/{damaged_id}", headers=operator)
    assert repaired.status_code == 200 and repaired.json()["id"] == damaged_id
    assert repaired.json()["failure"].get("uncertain", False) is False
