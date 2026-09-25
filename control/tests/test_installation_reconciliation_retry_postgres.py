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
    RecipeReconcileResult,
    canonical_message,
)
from vonk_control.agent_jobs import AgentJobService, StaleAgentAttempt
from vonk_control.models import AgentOperation, AgentOperationAttempt, Job

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

RECONCILE_CAPABILITIES = (
    "agent.runtime.rust.v1",
    "agent.lifecycle.resume.exact.v1",
    "recipe.reconcile",
    "recipe.reconcile.v1",
)


def _payload() -> RecipeReconcilePayload:
    return RecipeReconcilePayload(
        schema_version=1,
        node_id=NODE_A,
        installation_id=str(uuid.uuid4()),
        install_operation_id=str(uuid.uuid4()),
        install_operation_payload_sha256=COMMIT,
        plan_digest=COMMIT,
        recipe_revision_id=str(uuid.uuid4()),
        recipe_content_sha256=COMMIT,
        compiled_spec_canonical_sha256=COMMIT,
    )


def _result_envelope(
    claim: AgentClaim,
    state: Literal["succeeded", "failed", "waiting-for-operator"],
    result: Mapping[str, object],
) -> AgentResult:
    claim_document = claim.model_dump(mode="json")
    return AgentResult.model_validate_json(
        canonical_message(
            {
                **{
                    key: claim_document[key]
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
                "state": state,
                "result": result,
            }
        )
    )


def _reconciliation_receipt(payload: RecipeReconcilePayload) -> dict[str, object]:
    return RecipeReconcileResult(
        reconciled=True,
        node_id=payload.node_id,
        installation_id=payload.installation_id,
        install_operation_id=payload.install_operation_id,
        install_operation_payload_sha256=payload.install_operation_payload_sha256,
        plan_digest=payload.plan_digest,
        recipe_revision_id=payload.recipe_revision_id,
        recipe_content_sha256=payload.recipe_content_sha256,
        compiled_spec_canonical_sha256=payload.compiled_spec_canonical_sha256,
        removed_bytes=8192,
        cleanup_receipt_sha256="f" * 64,
    ).model_dump(mode="json")


@pytest.fixture
def pg_service(request: pytest.FixtureRequest):
    return request.getfixturevalue("postgres_service")


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
        30,
        protocol_version=3,
        capabilities=RECONCILE_CAPABILITIES,
    )
    assert first is not None
    assert first.operation_id == operation.id
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
                30,
                protocol_version=3,
                capabilities=RECONCILE_CAPABILITIES,
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
        assert stored.current_attempt == first.attempt
        assert stored.workload_intent_ordinal == 1
        assert stored.retry_disposition == "retry"
        assert stored.retry_disposition_attempt == first.attempt
        due = stored.retry_due_at
        assert due is not None
        assert parent_row.payload["workload_intent_ordinal"] == 1
        first_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == first.attempt,
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
            30,
            protocol_version=3,
            capabilities=RECONCILE_CAPABILITIES,
        )
        is None
    )
    clock.now = due.astimezone(UTC)
    second = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
        30,
        protocol_version=3,
        capabilities=RECONCILE_CAPABILITIES,
    )
    assert second is not None
    assert second.operation_id == first.operation_id == operation.id
    assert second.attempt == first.attempt + 1
    assert second.fence != first.fence
    assert second.payload == first.payload == payload

    # A delayed exact success from the old fence cannot overwrite the new owner.
    old_success = _result_envelope(first, "succeeded", _reconciliation_receipt(payload))
    with pytest.raises(StaleAgentAttempt):
        jobs.record_result(old_success)

    exact_success = _result_envelope(
        second, "succeeded", _reconciliation_receipt(payload)
    )
    jobs.record_result(exact_success)

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        parent_row = session.get(Job, parent_job.id)
        first_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == first.attempt,
            )
        )
        second_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == second.attempt,
            )
        )
        assert stored is not None and parent_row is not None
        assert stored.state == "succeeded"
        assert stored.current_attempt == second.attempt
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
        assert second_attempt.result == _reconciliation_receipt(payload)
