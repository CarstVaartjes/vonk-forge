"""A claim recovery must repair its durable operator explanation."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.jobs import JobService
from vonk_control.models import AgentOperation, Job
from vonk_control.operation_api import JobDetailResponse

from .agent_fences import fenced_operation
from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import claim_agent
from .test_agent_jobs import (
    COMMIT,
    NODE_A,
    NODE_B,
    STOP_PAYLOAD,
    STOP_RESULT,
    parent,
)
from .test_agent_jobs import (
    service as service,  # noqa: PLC0414 - expose the shared fixture
)


@pytest.mark.parametrize("sibling_blocked", (False, True))
def test_recovered_claim_repairs_parent_api_reason_after_restart(
    service, sibling_blocked
):
    """Retaining a cleared refusal or erasing a live sibling's cause is wrong."""
    jobs, sessions, clock = service
    predecessor = parent(sessions, clock)
    old = jobs.enqueue(predecessor.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    old_claim = claim_agent(jobs, NODE_A, "serial-a")
    assert old_claim is not None
    assert fenced_operation(sessions, old_claim).id == old.id
    if sibling_blocked:
        jobs.enqueue(
            predecessor.id,
            NODE_B,
            "recipe.stop",
            COMMIT,
            recipe_stop_payload(NODE_B, plan_digest=COMMIT),
        )
        assert claim_agent(jobs, NODE_B, "serial-b") is not None

    current = parent(sessions, clock)
    recovering = jobs.enqueue(current.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    sibling = jobs.enqueue(
        current.id,
        NODE_B,
        "recipe.stop",
        COMMIT,
        recipe_stop_payload(NODE_B, plan_digest=COMMIT),
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    if sibling_blocked:
        assert claim_agent(jobs, NODE_B, "serial-b") is None
    with sessions() as session:
        refused = session.get(AgentOperation, recovering.id)
        other = session.get(AgentOperation, sibling.id)
        persisted = session.get(Job, current.id)
        assert refused is not None and refused.current_attempt == 0
        assert other is not None and persisted is not None
        expected_reason = other.status_reason
        refused_reason = persisted.status_reason
        assert (expected_reason is not None) == sibling_blocked

    # Restart all owners on the same durable SQL store; the exact old receipt
    # clears the real predecessor, rather than editing any lifecycle state.
    restarted_engine = create_engine(sessions.kw["bind"].url)
    try:
        restarted_sessions = sessionmaker(restarted_engine, expire_on_commit=False)
        restarted = AgentJobService(restarted_sessions, clock=clock)
        codec = TokenCodec(b"c" * 32)
        headers = {
            "Authorization": "Bearer "
            + codec.issue(Actor("operator", "administrator"), ttl_seconds=1000, now=0)
        }
        app = create_app(
            jobs=JobService(restarted_sessions, clock=clock),
            tokens=codec,
            now=lambda: 10,
        )
        with TestClient(app) as client:
            before = client.get(f"/api/jobs/{current.id}", headers=headers)
            assert before.status_code == 200
            assert (
                JobDetailResponse.model_validate_json(before.content).status_reason
                == refused_reason
            )
            restarted.succeed(old_claim, STOP_RESULT)
            recovered = claim_agent(restarted, NODE_A, "serial-a")
            assert recovered is not None
            assert fenced_operation(restarted_sessions, recovered).id == recovering.id
            after = client.get(f"/api/jobs/{current.id}", headers=headers)
            assert after.status_code == 200
            assert (
                JobDetailResponse.model_validate_json(after.content).status_reason
                == expected_reason
            )
        with restarted_sessions() as session:
            row = session.get(AgentOperation, recovering.id)
            other = session.get(AgentOperation, sibling.id)
            assert (
                row is not None and row.state == "running" and row.status_reason is None
            )
            assert other is not None and other.state == "queued"
            assert other.status_reason == expected_reason
    finally:
        restarted_engine.dispose()
