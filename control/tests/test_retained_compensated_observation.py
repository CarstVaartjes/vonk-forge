"""Historical terminal display facts do not introduce writable lifecycle states."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState, OperationProgress
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.jobs import JobService
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
)
from vonk_control.operation_api import durable_operation_services


def test_retained_compensated_member_is_observed_without_rewriting_history(tmp_path):
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    node_id = "spk_" + "1" * 32
    engine = create_engine(f"sqlite:///{tmp_path / 'retained-history.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active"))
        job = Job(
            request_id="11111111-1111-4111-8111-111111111111",
            kind="reconcile",
            state=LifecycleState.RUNNING,
            actor="operator",
            authority_revision="a" * 64,
            targets=[node_id],
            payload_digest="b" * 64,
            payload={},
            current_attempt=1,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        operation_ids = []
        for index, (completed, rate) in enumerate(((100, 99.0), (25, 7.0)), 1):
            operation = AgentOperation(
                parent_job_id=job.id,
                node_id=node_id,
                kind="node.probe",
                payload_digest=f"{index:064x}",
                payload={},
                authority_revision="a" * 64,
                state=LifecycleState.RUNNING,
                current_attempt=1,
                created_at=now + timedelta(seconds=index),
                updated_at=now,
            )
            session.add(operation)
            session.flush()
            operation_ids.append(operation.id)
            progress = OperationProgress(
                phase="copying",
                completed_bytes=completed,
                total_bytes=100,
                total_bytes_known=True,
                bytes_per_second=rate,
                smoothed_bytes_per_second=rate,
                eta_seconds=10.0,
                activity="active",
                observed_at=now.isoformat(),
                last_progress_at=now.isoformat(),
            )
            session.add(
                AgentOperationAttempt(
                    operation_id=operation.id,
                    attempt=1,
                    fence=f"00000000-0000-4000-8000-{index:012d}",
                    lease_deadline=now + timedelta(minutes=1),
                    agent_certificate_serial=f"serial-{index}",
                    state=LifecycleState.RUNNING,
                    progress=progress.model_dump(mode="json", exclude_none=True),
                )
            )
    # Import a retained historical fact below current producer semantics. This
    # does not manufacture a new receipt or authorize any physical effect.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE agent_operations SET state=:state WHERE id=:id"),
            {"state": "compensated", "id": operation_ids[0]},
        )

    def retained_rows():
        with sessions() as session:
            return list(
                session.execute(
                    select(
                        AgentOperation.id,
                        AgentOperation.state,
                        AgentOperation.updated_at,
                        AgentOperation.payload,
                        AgentOperationAttempt.fence,
                        AgentOperationAttempt.state,
                        AgentOperationAttempt.progress,
                    )
                    .join(AgentOperationAttempt)
                    .order_by(AgentOperation.id)
                )
            )

    original = retained_rows()
    codec = TokenCodec(b"k" * 32)
    services = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: now,
        cursors=codec.cursor_codec(),
    )
    app = create_app(
        jobs=JobService(sessions, clock=lambda: now),
        operations=services,
        tokens=codec,
        now=lambda: 10,
    )
    token = codec.issue(Actor("operator", "operator"), ttl_seconds=100, now=0)
    with TestClient(app) as client:
        response = client.get(
            f"/api/jobs/{job.id}", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    document = response.json()
    historical = next(
        member for member in document["operations"] if member["id"] == operation_ids[0]
    )
    assert historical["state"] == "compensated"
    assert historical["progress"]["completed_bytes"] == 100
    for field in (
        "activity",
        "bytes_per_second",
        "smoothed_bytes_per_second",
        "eta_seconds",
    ):
        assert field not in historical["progress"]
    aggregate = document["progress"]["operation"]
    assert aggregate["completed_bytes"] == 125
    assert aggregate["bytes_per_second"] == 7.0
    assert aggregate["smoothed_bytes_per_second"] == 7.0
    assert document["progress"]["completed"] == 1
    assert document["progress"]["running"] == 1
    assert retained_rows() == original
    with pytest.raises(ValueError):
        LifecycleState("compensated")
