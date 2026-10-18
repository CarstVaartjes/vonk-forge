"""A named model layout fault survives the real queue and reaches Fleet warnings."""

import uuid
from datetime import UTC, timedelta
from pathlib import Path

from sqlalchemy import Engine
from vonk_agent_protocol import (
    AgentFailureKind,
    AgentResult,
    AgentResultState,
    LifecycleState,
    ProjectionCode,
    canonical_message,
)
from vonk_agent_protocol.contracts import AgentFailureResult, AgentInstallResult
from vonk_agent_protocol.failure_evidence import (
    FailureDiagnostics,
    ModelStoreLayoutFault,
)
from vonk_agent_protocol.lifecycle_vocabulary import FailureCode
from vonk_control.agent_jobs import AgentJobService
from vonk_control.fleet_projection import FleetProjection

from .agent_fences import fenced_operation
from .runtime_identity_support import claim_agent
from .test_recipe_operations import NOW, setup_services


def _assert_layout_recovery(
    tmp_path: Path,
    engine: Engine | None = None,
) -> None:
    """Catches log-only errors, lost path/errno and a permanent recipe refusal."""
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=engine
    )
    now = [NOW]
    service._clock = lambda: now[0]
    jobs = AgentJobService(sessions, clock=lambda: now[0])
    jobs.set_result_consumer(service.consume_agent_result)
    plan = service.preview_install(mapping_id, build_id)
    installation = service.install(
        plan, plan_digest=plan.plan_digest, actor="test", request_id=str(uuid.uuid4())
    )
    claim = claim_agent(jobs, nodes[0], "serial-0")
    assert claim is not None
    evidence = AgentFailureResult(
        error_code=FailureCode.MODEL_STORE_LAYOUT,
        reason="model store layout needs attention",
        status=AgentResultState.FAILED.value,
        failure_kind=AgentFailureKind.TEMPORARY_DEPENDENCY,
        retry_after_seconds=5,
        diagnostics=FailureDiagnostics.model_validate_json(
            (
                Path(__file__).parents[2]
                / "agent_protocol/src/vonk_agent_protocol/vectors/failure-diagnostics-v1.json"
            ).read_bytes()
        ).model_copy(
            update={
                FailureCode.MODEL_STORE_LAYOUT.value: ModelStoreLayoutFault(
                    path="installations/id/models/weights.safetensors", errno=18
                )
            }
        ),
    )
    jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": claim.fence,
                    "state": AgentResultState.FAILED,
                    "result": evidence,
                }
            )
        )
    )
    # Read through a new projection instance: only SQL receipts can supply it.
    snapshot = FleetProjection(sessions, clock=lambda: now[0]).read()
    node = next(node for node in snapshot.nodes if node.id == nodes[0])
    notice = next(
        w for w in node.warnings if w.code == ProjectionCode.MODEL_STORE_LAYOUT
    )
    assert notice.model_store_layout is not None
    assert (
        notice.model_store_layout.path == "installations/id/models/weights.safetensors"
    )
    assert nodes[0] in notice.detail
    assert "errno=18" in notice.detail and "weights.safetensors" in notice.detail
    assert notice.recommendation is not None and "filesystem" in notice.recommendation
    assert service.get(installation.id).state != LifecycleState.FAILED

    parked = fenced_operation(sessions, claim)
    assert parked.next_action_at is not None and parked.recovery_deadline is not None
    deadline = parked.recovery_deadline
    now[0] = parked.next_action_at.replace(tzinfo=UTC) + timedelta(seconds=1)
    # Recreate the queue owner and resume the original persisted operation.
    jobs = AgentJobService(sessions, clock=lambda: now[0])
    jobs.set_result_consumer(service.consume_agent_result)
    repaired = claim_agent(jobs, nodes[0], "serial-0")
    assert repaired is not None
    assert any(
        warning.code == ProjectionCode.MODEL_STORE_LAYOUT
        for node in FleetProjection(sessions, clock=lambda: now[0]).read().nodes
        for warning in node.warnings
    )
    assert fenced_operation(sessions, repaired).recovery_deadline == deadline
    assert (
        fenced_operation(sessions, repaired).id == fenced_operation(sessions, claim).id
    )
    jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": repaired.fence,
                    "state": AgentResultState.SUCCEEDED,
                    "result": AgentInstallResult(installed_bytes=120),
                }
            )
        )
    )
    assert service.get(installation.id).state == LifecycleState.SUCCEEDED
    assert all(
        w.code != ProjectionCode.MODEL_STORE_LAYOUT
        for n in FleetProjection(sessions, clock=lambda: now[0]).read().nodes
        for w in n.warnings
    )


def test_layout_fault_is_visible_after_restart_then_clears_on_same_order_success(
    tmp_path: Path,
) -> None:
    """Catches a log-only layout fault and losing its receipt across queue restart."""
    _assert_layout_recovery(tmp_path)


def test_postgres_layout_receipt_reaches_fleet_and_recovers(
    tmp_path: Path, postgres_engine: Engine
) -> None:
    """Catches JSON/SQL receipt differences that hide the layout fault on PostgreSQL."""
    _assert_layout_recovery(tmp_path, postgres_engine)
