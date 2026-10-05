from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC
from typing import Literal

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentClaim,
    AgentResult,
    RecipeReconcilePayload,
    canonical_message,
)
from vonk_control.agent_jobs import AgentJobService, StaleAgentAttempt
from vonk_control.models import AgentOperation, AgentOperationAttempt, Job

from .agent_fences import fenced_attempt, fenced_operation
from .runtime_identity_support import claim_agent
from .test_agent_jobs_postgres import (
    COMMIT,
    NODE_A,
    parent,
)
from .test_agent_jobs_postgres import (
    service as postgres_service,  # noqa: F401 - imported fixture reused below.
)

pytestmark = pytest.mark.lane


def _payload() -> RecipeReconcilePayload:
    return RecipeReconcilePayload(installation_id=str(uuid.uuid4()), plan_digest=COMMIT)


def _result_envelope(
    claim: AgentClaim,
    state: Literal["succeeded", "failed", "waiting-for-operator"],
    result: Mapping[str, object],
) -> AgentResult:
    claim_document = claim.model_dump(mode="json")
    return AgentResult.model_validate_json(
        canonical_message(
            {
                **{key: claim_document[key] for key in ("fence",)},
                "state": state,
                "result": result,
            }
        )
    )


@pytest.fixture
def pg_service(postgres_service):  # noqa: F811 - the imported fixture, requested statically
    # A static request keeps postgres_engine in this test's fixture closure,
    # so the suite marks it ``postgres`` and starts the shared server for it.
    return postgres_service


@pytest.mark.parametrize(
    "interruption",
    ("temporary-failure", "agent-restart", "lease-expiry"),
    ids=("temporary-failure", "agent-restart-interrupted", "lease-expiry"),
)
def test_postgres_recipe_reconcile_retries_same_intent_and_fences_old_result(
    pg_service,
    interruption: Literal["temporary-failure", "agent-restart", "lease-expiry"],
) -> None:
    sessions, clock = pg_service
    parent_job = parent(sessions, clock)
    with sessions.begin() as session:
        stored_parent = session.get(Job, parent_job.id)
        assert stored_parent is not None
        stored_parent.targets = [NODE_A]

    jobs = AgentJobService(sessions, clock=clock)
    payload = _payload()
    operation = jobs.enqueue(
        parent_job.id,
        NODE_A,
        "recipe.reconcile",
        COMMIT,
        payload.model_dump(mode="json"),
    )
    first = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
    )
    assert first is not None
    assert fenced_operation(sessions, first).id == operation.id
    assert first.payload == payload

    if interruption == "temporary-failure":
        failure_state = "failed"
        failure = {
            "status": "failed",
            "error_code": "reconciliation_temporarily_unavailable",
            "failure_kind": "temporary-dependency",
            "reason": "the exact installation is temporarily busy",
        }
    elif interruption == "agent-restart":
        failure_state = "waiting-for-operator"
        failure = {
            "error_code": "agent_restart_interrupted",
            "failure_kind": "uncertain-effect",
            "uncertain": True,
            "reason": "the agent restarted before recording reconciliation",
        }
    else:
        failure_state = None
        failure = None
        # Model a Controller that lost the executor before any result arrived.
        # The lease deadline is durable; expiry must schedule the same exact
        # operation for a new fence without treating absence as success.
        clock.now = first.deadline.astimezone(UTC)
        assert (
            claim_agent(
                jobs,
                NODE_A,
                "serial-a",
            )
            is None
        )
    if failure is not None:
        assert failure_state is not None
        jobs.record_result(_result_envelope(first, failure_state, failure))

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        parent_row = session.get(Job, parent_job.id)
        assert stored is not None and parent_row is not None
        assert stored.state == "waiting-for-operator"
        assert stored.current_attempt == fenced_attempt(sessions, first).attempt
        assert stored.workload_intent_ordinal == 1
        assert stored.next_action_at is not None
        due = stored.next_action_at
        assert due is not None
        assert parent_row.payload["workload_intent_ordinal"] == 1
        first_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt
                == fenced_attempt(sessions, first).attempt,
            )
        )
        assert first_attempt is not None
        if failure is None:
            assert first_attempt.state == "expired"
            assert first_attempt.result is None
        else:
            assert failure_state is not None
            assert first_attempt.state == failure_state
            assert isinstance(first_attempt.result, dict)
            assert first_attempt.result["error_code"] == failure["error_code"]

    # A newly constructed service models Controller process recovery over the
    # same PostgreSQL authority. It must wait for the persisted bounded due time.
    jobs = AgentJobService(sessions, clock=clock)
    assert (
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
        )
        is None
    )
    clock.now = due.astimezone(UTC)
    second = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
    )
    assert second is not None
    assert fenced_operation(sessions, second).id == operation.id
    assert (
        fenced_attempt(sessions, second).attempt
        == fenced_attempt(sessions, first).attempt + 1
    )
    assert second.fence != first.fence
    assert second.payload == first.payload == payload

    # A delayed exact success from the old fence cannot overwrite the new owner.
    old_success = _result_envelope(first, "succeeded", {})
    with pytest.raises(StaleAgentAttempt):
        jobs.record_result(old_success)

    exact_success = _result_envelope(second, "succeeded", {})
    jobs.record_result(exact_success)

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        parent_row = session.get(Job, parent_job.id)
        first_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt
                == fenced_attempt(sessions, first).attempt,
            )
        )
        second_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt
                == fenced_attempt(sessions, second).attempt,
            )
        )
        assert stored is not None and parent_row is not None
        assert stored.state == "succeeded"
        assert stored.current_attempt == fenced_attempt(sessions, second).attempt
        assert stored.workload_intent_ordinal == 1
        assert stored.payload == payload.model_dump(mode="json")
        assert parent_row.state == "succeeded"
        assert first_attempt is not None
        expected_first_state = (
            "failed" if interruption == "temporary-failure" else "expired"
        )
        assert first_attempt.state == expected_first_state
        if failure is None:
            assert first_attempt.result is None
        else:
            assert first_attempt.result is not None
        assert second_attempt is not None and second_attempt.state == "succeeded"
        assert second_attempt.result == {}
