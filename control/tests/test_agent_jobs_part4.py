from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from vonk_agent_protocol import AgentOperation as ProtocolAgentOperation
from vonk_agent_protocol import (
    LifecycleState,
)
from vonk_control.lifecycle.agent_operation import (
    AgentOperationAdapter,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Job,
)

from . import agent_jobs_support as _support
from .agent_fences import fenced_operation
from .agent_jobs_support import (
    COMMIT,
    NODE_A,
    STOP_PAYLOAD,
    STOP_RESULT,
    parent,
    superseded_order_payload,
)
from .node_contacts import contact_row
from .runtime_identity_support import (
    claim_agent,
)

service = _support.service


def test_gpu_fault_between_admission_and_claim_waits_then_recovers(service) -> None:
    # Catches dispatching a previously accepted launch after the GPU broke;
    # cleanup must remain claimable and a healthy observation must unblock it.
    from vonk_agent_protocol import ProjectionCode
    from vonk_agent_protocol.inventory import GpuHealth
    from vonk_control.inventory.repository import (
        InventoryRepository,
        NodeInventoryReport,
    )

    jobs, sessions, clock = service
    repo = InventoryRepository(sessions)
    evidence = NodeInventoryReport(
        schema_version=1,
        node_id=NODE_A,
        observed_at=clock.now,
        disk_total_bytes=1000,
        disk_free_bytes=900,
        host_memory_total_bytes=1000,
        host_memory_free_bytes=900,
        gpu_memory_total_bytes=1000,
        gpu_memory_free_bytes=900,
        gpu_count=1,
        memory_pool="shared",
        capabilities=[],
        artifact_store_read_only=False,
        nvidia_driver_version="unknown",
        container_runtime_version="unknown",
        gpu_health=GpuHealth.NEEDS_RESET,
    )
    repo.record(evidence)
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.RECIPE_START,
        COMMIT,
        superseded_order_payload(clock),
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    with sessions() as session:
        waiting = session.get(AgentOperation, operation.id)
        assert waiting is not None
        assert waiting.current_attempt == 0
        assert ProjectionCode.NODE_GPU_REQUIRES_RESET in waiting.status_reason
        assert waiting.next_action_at is not None
        deadline = waiting.recovery_deadline
    from vonk_control.agent_jobs.waits import record_queue_waits

    with sessions.begin() as session:
        waiting = session.get(AgentOperation, operation.id)
        assert waiting is not None
        record_queue_waits(session, [waiting], clock.now)
        assert waiting.wait_evidence is not None
        assert waiting.wait_evidence.reason == ProjectionCode.NODE_GPU_REQUIRES_RESET
        assert waiting.wait_evidence.blockers[0].node_ids == [NODE_A]
    cleanup = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.RECIPE_STOP,
        COMMIT,
        STOP_PAYLOAD,
    )
    stop = claim_agent(jobs, NODE_A, "serial-a")
    assert stop is not None
    assert fenced_operation(sessions, stop).id == cleanup.id
    jobs.succeed(stop, STOP_RESULT)
    clock.advance(seconds=5)
    repo.record(
        evidence.model_copy(
            update={"observed_at": clock.now, "gpu_health": GpuHealth.HEALTHY}
        )
    )
    clock.advance(seconds=1)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    assert fenced_operation(sessions, claim).id == operation.id
    with sessions() as session:
        current = session.get(AgentOperation, operation.id)
        assert current is not None
        assert current.recovery_deadline == deadline


def test_ended_hold_clears_the_stamped_deadline_of_install_build_and_job_not_start() -> (
    None
):
    # Catches an install or build that keeps what remained of a dispatch hold.

    stamped = datetime(2026, 1, 1, tzinfo=UTC)
    for kind, cleared in (
        (ProtocolAgentOperation.RECIPE_INSTALL, True),
        (ProtocolAgentOperation.RECIPE_BUILD, True),
        (ProtocolAgentOperation.RECIPE_JOB_RUN, True),
        (ProtocolAgentOperation.RECIPE_START, False),
    ):
        operation = AgentOperation(
            kind=kind.value, current_attempt=0, recovery_deadline=stamped
        )
        AgentOperationAdapter.clear_hold_deadline(operation)
        assert (operation.recovery_deadline is None) is cleared
    later = AgentOperation(
        kind=ProtocolAgentOperation.RECIPE_INSTALL.value,
        current_attempt=1,
        recovery_deadline=stamped,
    )
    AgentOperationAdapter.clear_hold_deadline(later)
    assert later.recovery_deadline == stamped


def test_reusable_preflight_survives_operation_history_collection(service) -> None:
    """Catches treating a historical command receipt as current capability storage."""
    from sqlalchemy import delete
    from vonk_control.runtime.preflight import (
        admission_blockers,
        latest_result,
        request_digest,
    )

    from .test_runtime_preflight import request as preflight_request
    from .test_runtime_preflight import result as passing_result

    queue, sessions, clock = service
    request = preflight_request()
    digest = request_digest(request)
    job = parent(sessions, clock)
    operation = queue.enqueue(
        job.id,
        NODE_A,
        ProtocolAgentOperation.RUNTIME_PREFLIGHT,
        COMMIT,
        request.model_dump(mode="json"),
    )
    claim = claim_agent(queue, NODE_A, "serial-a", preflight_fingerprint="a" * 64)
    assert claim is not None
    result = passing_result(request, observed_at=int(clock.now.timestamp()))
    queue.succeed(claim.fence, result)
    with sessions.begin() as session:
        session.execute(
            delete(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        )
        session.execute(delete(AgentOperation).where(AgentOperation.id == operation.id))
    with sessions() as session:
        retained = latest_result(session, NODE_A, requirements_sha256=digest)
        assert retained is not None and retained.result == result
        assert not admission_blockers(request, retained, current_fingerprint="a" * 64)
        assert admission_blockers(request, retained, current_fingerprint="b" * 64)


def test_failed_child_end_wake_preserves_committed_receipt(service):
    """A failed owner wake cannot turn an already committed report into loss."""
    from vonk_control.run_switch_operations.child_wake import ChildEndWake
    from vonk_control.runtime.preflight import latest_result, request_digest

    from .test_runtime_preflight import request as preflight_request
    from .test_runtime_preflight import result as passing_result

    queue, sessions, clock = service
    request = preflight_request()
    job = parent(sessions, clock)
    queue.enqueue(
        job.id,
        NODE_A,
        ProtocolAgentOperation.RUNTIME_PREFLIGHT,
        COMMIT,
        request.model_dump(mode="json"),
    )

    def lost_wake() -> datetime:
        raise OSError("injected completion wake loss")

    ChildEndWake.install(sessions, lost_wake)
    claim = claim_agent(queue, NODE_A, "serial-a", preflight_fingerprint="a" * 64)
    assert claim is not None
    queue.succeed(
        claim.fence, passing_result(request, observed_at=int(clock.now.timestamp()))
    )
    with sessions() as session:
        receipt = latest_result(
            session, NODE_A, requirements_sha256=request_digest(request)
        )
        assert receipt is not None and receipt.received_at == clock.now
        committed = session.get(Job, job.id)
        assert committed is not None and committed.state == LifecycleState.SUCCEEDED


@pytest.mark.parametrize("contact_path", ("claim", "heartbeat"))
def test_controller_clock_rollback_replaces_pinned_contact(
    service, contact_path
) -> None:
    """Catches max(stored, now) keeping a future contact after clock correction."""
    jobs, sessions, clock = service
    claim = None
    if contact_path == "heartbeat":
        jobs.enqueue(
            parent(sessions, clock).id,
            NODE_A,
            ProtocolAgentOperation.RECIPE_STOP,
            COMMIT,
            STOP_PAYLOAD,
        )
        claim = claim_agent(jobs, NODE_A, "serial-a")
        assert claim is not None
    # The clock steps back inside the certificate validity interval.
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        contact_row(node).last_seen_at = clock() + timedelta(hours=1)
    if claim is None:
        claim_agent(jobs, NODE_A, "serial-a")
    else:
        jobs.heartbeat(claim, None, 30)
    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        seen = contact_row(node).last_seen_at
        assert seen is not None and seen.replace(tzinfo=UTC) == clock()
