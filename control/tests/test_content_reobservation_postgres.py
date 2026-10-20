"""Lost content receipts adopt only the exact surviving owner on PostgreSQL."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentInstallResult,
    AgentResult,
    AgentResultState,
    CompiledExecutionPlan,
    LifecycleState,
    ObservationCause,
    RecipeBuildCleanupEvidence,
    RecipeBuildCleanupRequest,
    RecipeBuildEvidence,
    RecipeBuildRequest,
    RecipeReconcilePayload,
    RecipeReconcileResult,
    RecipeUninstallPayload,
    RecipeUninstallResult,
    canonical_message,
)
from vonk_agent_protocol import (
    AgentOperation as Kind,
)
from vonk_agent_protocol.contracts import AgentPayload, AgentResultPayload
from vonk_agent_protocol.recipe_operations import RecipeInstallPayload
from vonk_agent_protocol.runtime_preflight import (
    RuntimePreflightRequest,
    RuntimePreflightResult,
)
from vonk_control.agent_jobs import AgentJobService, StaleAgentFence
from vonk_control.models import AgentNode, AgentOperation, AgentOperationAttempt, Job

from . import test_agent_jobs_postgres as fixtures
from .runtime_identity_support import claim_agent
from .test_agent_jobs_postgres import COMMIT, NODE_A, parent


@pytest.fixture
def service(postgres_engine):
    return fixtures.agent_queue_world(postgres_engine)


def _content(kind: Kind) -> tuple[AgentPayload, AgentResultPayload]:
    plan = CompiledExecutionPlan.model_validate_json(
        (
            Path(__file__).parents[2]
            / "agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
        ).read_bytes()
    )
    installation_id = "00000000-0000-4000-8000-000000000001"
    if kind is Kind.RECIPE_INSTALL:
        return RecipeInstallPayload(
            installation_id=installation_id,
            plan_digest="b" * 64,
            expected_bytes=1,
            compiled_execution_plan=plan,
        ), AgentInstallResult(installed_bytes=1)
    if kind is Kind.RECIPE_RECONCILE:
        return RecipeReconcilePayload(
            installation_id=installation_id, plan_digest="b" * 64
        ), RecipeReconcileResult()
    if kind is Kind.RECIPE_UNINSTALL:
        return RecipeUninstallPayload(
            installation_id=installation_id,
            plan_digest="b" * 64,
            recipe_content_sha256=plan.identity.recipe_revision_sha256,
            cleanup_model_content_sha256=None,
        ), RecipeUninstallResult()
    if kind is Kind.RECIPE_BUILD_CLEANUP:
        return RecipeBuildCleanupRequest(
            build_id=str(uuid4()), operation_id=str(uuid4())
        ), RecipeBuildCleanupEvidence()
    if kind is Kind.RUNTIME_PREFLIGHT:
        return RuntimePreflightRequest(
            source_build=False,
            minimum_free_bytes=0,
            fabric_connectivity="none",
            fabric_minimum_mbps=0,
        ), RuntimePreflightResult(fingerprint="a" * 64, observed_at=1, findings=[])
    assert kind is Kind.RECIPE_BUILD
    vector = json.loads(
        (
            Path(__file__).parents[2]
            / "agent_protocol/src/vonk_agent_protocol/vectors/recipe-build-claim-v1.json"
        ).read_text()
    )
    return RecipeBuildRequest.model_validate_json(
        canonical_message(vector["base_payload"])
    ), RecipeBuildEvidence(
        image_bytes=1, image_digest="sha256:" + "a" * 64, oci_layout_sha256="b" * 64
    )


def _accepted(service, kind: Kind = Kind.RECIPE_INSTALL):
    sessions, clock = service
    queue = AgentJobService(sessions, clock=clock)
    owner = parent(sessions, clock)
    payload, result = _content(kind)
    operation = queue.enqueue(owner.id, NODE_A, kind, COMMIT, payload)
    claim = claim_agent(queue, NODE_A, "serial-a")
    assert claim is not None
    receipt = AgentResult(
        fence=claim.fence,
        state=AgentResultState.SUCCEEDED,
        result=result,
    )
    clock.advance(seconds=60)
    return queue, sessions, clock, owner, operation, claim, receipt


@pytest.mark.parametrize(
    "kind",
    sorted(
        set(Kind)
        - {
            Kind.ARTIFACT_DISTRIBUTION,
            Kind.RECIPE_START,
            Kind.RECIPE_STOP,
            Kind.RECIPE_JOB_RUN,
            Kind.AGENT_UPGRADE,
        }
    ),
)
def test_lost_content_report_adopts_after_lease_without_reexecution_or_renewal(
    service, kind
):
    # Wrong implementation: only distribution could adopt a completed effect;
    # an installed child was reissued after its report was lost.
    queue, sessions, _clock, _owner, operation, claim, receipt = _accepted(
        service, kind
    )
    with sessions() as session:
        before = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert before is not None
        deadline = before.lease_deadline
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        contact = node.last_seen_at
    assert queue.record_late_result(receipt)
    with sessions() as session:
        row = session.get(AgentOperation, operation.id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert row is not None and row.state == LifecycleState.SUCCEEDED
        assert attempt is not None
        assert attempt.lease_deadline == deadline
        node = session.get(AgentNode, NODE_A)
        assert node is not None and node.last_seen_at == contact
    assert claim_agent(queue, NODE_A, "serial-a") is None


@pytest.mark.parametrize(
    "change", ["identity", "revoked", "attempt", "cancel", "owner"]
)
def test_late_install_receipt_rejects_changed_authority(service, change):
    # Wrong implementation: a syntactically valid receipt overwrote a newer
    # attempt, cancelled parent, revoked node or changed effect identity.
    queue, sessions, clock, owner, operation, _claim, receipt = _accepted(service)
    with sessions.begin() as session:
        row = session.get(AgentOperation, operation.id)
        node = session.get(AgentNode, NODE_A)
        job = session.get(Job, owner.id)
        assert row is not None and node is not None and job is not None
        if change == "identity":
            row.authority_revision = "f" * 64
        elif change == "revoked":
            node.revoked_at = clock.now
        elif change == "attempt":
            row.current_attempt += 1
        elif change == "cancel":
            job.state = LifecycleState.CANCELLED
        else:
            node.workload_intent_ordinal += 1
    try:
        queue.record_late_result(receipt)
    except StaleAgentFence as error:
        assert str(error)
    with sessions() as session:
        row = session.get(AgentOperation, operation.id)
        assert row is not None and row.state != LifecycleState.SUCCEEDED


@pytest.mark.parametrize("parked", [False, True])
def test_worker_adopts_retained_install_receipt_without_contact(service, parked):
    # Wrong implementation: adoption ran only at ingress, so a retained
    # authenticated receipt on a completely idle fleet stayed parked forever.
    queue, sessions, clock, _owner, operation, claim, receipt = _accepted(service)
    with sessions.begin() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt is not None
        if parked:
            row = session.get(AgentOperation, operation.id)
            assert row is not None
            row.state = LifecycleState.OBSERVING
        attempt.state = LifecycleState.OBSERVING
        attempt.observation_cause = ObservationCause.LEASE_LAPSED
        attempt.result = receipt.result.model_dump(mode="json")
    queue = AgentJobService(sessions, clock=clock)
    assert queue.reconcile_orders()
    with sessions() as session:
        row = session.get(AgentOperation, operation.id)
        assert row is not None and row.state == LifecycleState.SUCCEEDED
