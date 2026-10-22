"""Disk admission must distinguish a build's intent from an executing writer."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from vonk_agent_protocol import AgentOperation as WireOperation
from vonk_agent_protocol import (
    LifecycleState,
    ProgressPhase,
    ReservationState,
    canonical_message,
)
from vonk_control.job_documents import RecipeBuildParent
from vonk_control.models import AgentOperation, Job, RecipeBuild, ResourceReservation
from vonk_control.owner_liveness import OwnerKind
from vonk_control.recipe_execution_contract import parse_stored_build_plan

from .test_disk_reservations import _record_disk
from .test_recipe_operations import NOW, setup_services
from .test_run_switch_operations import RecordingArtifactExecutor, _request, _service


def _inject_build(sessions, ready_build, node_id, state, *, failed_row=False):
    """An unrelated build holds all the free disk; its parent remains open."""
    build_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    with sessions.begin() as session:
        source = session.get(RecipeBuild, ready_build)
        assert source is not None
        plan = parse_stored_build_plan(source.plan).model_copy(
            update={"build_id": build_id, "build_input_sha256": "f" * 64}
        )
        build = RecipeBuild(
            id=build_id,
            recipe_revision_id=source.recipe_revision_id,
            builder_node_id=node_id,
            source_bundle_sha256=source.source_bundle_sha256,
            build_input_sha256=plan.build_input_sha256,
            state=(
                LifecycleState.FAILED
                if failed_row or state == LifecycleState.FAILED
                else ProgressPhase.BUILDING
            ),
            policy_report=source.policy_report,
            plan=plan.model_dump(mode="json"),
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(build)
        session.add(
            Job(
                id=job_id,
                kind=WireOperation.RECIPE_BUILD,
                state=LifecycleState.RUNNING,
                request_id=str(uuid.uuid4()),
                actor="admin",
                authority_revision="a" * 64,
                targets=[node_id],
                payload_digest="b" * 64,
                payload=RecipeBuildParent(
                    schema_version=1,
                    owner_kind=OwnerKind.RECIPE_BUILD.value,
                    owner_id=build_id,
                    plan_digest=plan.build_input_sha256,
                ).model_dump(mode="json"),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add(
            AgentOperation(
                parent_job_id=job_id,
                node_id=node_id,
                kind=WireOperation.RECIPE_BUILD,
                state=state,
                current_attempt=0 if state == LifecycleState.QUEUED else 1,
                authority_revision="a" * 64,
                payload_digest="b" * 64,
                payload=plan.model_dump(mode="json"),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            ResourceReservation(
                node_id=node_id,
                kind="disk",
                resource_key=plan.build_input_sha256,
                amount_bytes=2_000,
                owner_kind=OwnerKind.RECIPE_BUILD,
                owner_id=build_id,
                state=ReservationState.ACTIVE,
                plan_digest=plan.build_input_sha256,
                created_at=NOW,
            )
        )
    return build_id


@pytest.mark.parametrize(
    ("state", "failed_row"),
    [
        (LifecycleState.FAILED, True),
        (LifecycleState.BACKOFF, False),
        (LifecycleState.RUNNING, False),
        (LifecycleState.OBSERVING, False),
        (LifecycleState.QUEUED, False),
        (LifecycleState.RUNNING, True),
        (LifecycleState.BACKOFF, True),
    ],
)
@pytest.mark.parametrize("fresh_disk", [False, True])
def test_pending_load_fits_after_build_stops_but_never_borrows_live_disk(
    tmp_path, postgres_engine, state, failed_row, fresh_disk
):
    """Catches full charges for dead/backoff builds and discounts of live writers."""
    sessions, lifecycle, _queue, _mapping, ready_build, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    later = NOW + timedelta(seconds=60)
    _record_disk(
        sessions,
        nodes[0],
        at=later,
        free=2_000,
        observed_at=later if fresh_disk else NOW + timedelta(seconds=1),
    )
    service = _service(sessions, later, lifecycle, RecordingArtifactExecutor())
    request = _request(sessions, nodes[0])
    before = service.preview(request, actor="admin")
    assert before.fit.allowed
    _inject_build(sessions, ready_build, nodes[0], state, failed_row=failed_row)

    after = service.preview(request, actor="admin")

    released = fresh_disk and state in {LifecycleState.FAILED, LifecycleState.BACKOFF}
    assert after.fit.allowed is released
    if released:
        assert after.fit.nodes[0].disk_free_after_bytes == (
            before.fit.nodes[0].disk_free_after_bytes
        )


@pytest.mark.parametrize("capacity_returns", [True, False])
def test_retry_reacquires_disk_before_dispatch_and_resumes_when_space_returns(
    tmp_path, postgres_engine, capacity_returns
):
    """A released envelope cannot let a due retry oversubscribe a later load."""
    from sqlalchemy import select
    from vonk_agent_protocol import (
        AgentFailureKind,
        AgentResult,
        AgentResultState,
        FailureCode,
    )
    from vonk_agent_protocol.contracts import AgentFailureResult
    from vonk_agent_protocol.runtime_preflight import RuntimePreflightRequest
    from vonk_control.agent_jobs import AgentJobService
    from vonk_control.disk_reservations import outstanding_disk_reservation_bytes
    from vonk_control.inventory_repository import (
        InventoryRepository,
        NodeInventoryReport,
    )
    from vonk_control.lifecycle_preflight import LifecyclePreflight
    from vonk_control.models import AgentCertificate

    from .test_build_cancellation_recovery import _services
    from .test_runtime_preflight import result as preflight_result

    sessions, _builds, operations, _storage, now, node_id, revision, plan = _services(
        tmp_path, postgres_engine
    )
    clock = [now]
    agents = AgentJobService(sessions, clock=lambda: clock[0])
    with sessions.begin() as session:
        session.add(
            AgentCertificate(
                serial="builder-serial",
                node_id=node_id,
                not_before=now - timedelta(seconds=1),
                not_after=now + timedelta(hours=2),
                fingerprint="builder-fingerprint",
            )
        )
    parent = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )

    def claim():
        return agents.claim(
            node_id,
            "builder-serial",
            runtime_identity={
                "architecture": "linux-arm64",
                "semantic_version": "1.2.3",
                "build_digest": "sha256:" + "a" * 64,
                "binary_digest": "1" * 64,
            },
        )

    first = claim()
    assert first is not None
    agents.record_result(
        AgentResult(
            fence=first.fence,
            state=AgentResultState.FAILED,
            result=AgentFailureResult(
                status=AgentResultState.FAILED.value,
                error_code=FailureCode.RECIPE_BUILD_FAILED,
                reason="fault injection: build import ran out of disk",
                failure_kind=AgentFailureKind.RESOURCE_PREREQUISITE,
                retry_after_seconds=1,
            ),
        )
    )
    with sessions() as session:
        order = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == parent.id)
        )
        assert order is not None and order.state == LifecycleState.BACKOFF
        assert order.next_action_at is not None
        assert order.recovery_deadline is not None
        deadline = order.recovery_deadline
        clock[0] = order.next_action_at + timedelta(seconds=31)
        assert (
            outstanding_disk_reservation_bytes(
                session, node_id, inventory_observed_at=now
            )
            > 0
        )

    def inventory(free):
        InventoryRepository(sessions, clock=lambda: clock[0]).record(
            NodeInventoryReport.model_validate_json(
                canonical_message(
                    {
                        "node_id": node_id,
                        "observed_at": clock[0],
                        "disk_total_bytes": 2 * 1024**4,
                        "disk_free_bytes": free,
                        "host_memory_total_bytes": 100_000,
                        "host_memory_free_bytes": 80_000,
                        "gpu_memory_total_bytes": 100_000,
                        "gpu_memory_free_bytes": 80_000,
                        "gpu_count": 1,
                        "artifact_store_read_only": False,
                        "capabilities": (WireOperation.RECIPE_BUILD, "runtime.vonk.v1"),
                        "memory_pool": "separate",
                        "schema_version": 1,
                        "nvidia_driver_version": "unknown",
                        "container_runtime_version": "unknown",
                    }
                )
            )
        )

    inventory(0)
    with sessions() as session:
        assert (
            outstanding_disk_reservation_bytes(
                session, node_id, inventory_observed_at=clock[0]
            )
            == 0
        )
    assert claim() is None
    with sessions() as session:
        order = session.get(AgentOperation, order.id)
        assert order is not None and order.current_attempt == 1
        assert order.state == LifecycleState.BACKOFF
        assert order.status_reason is not None
        assert "disk" in order.status_reason

    # The old retry must not head-block the load's newly eligible preflight,
    # including a zero-wait poll made after the retry is due again.
    clock[0] += timedelta(seconds=3)
    checkpoint, _reason = LifecyclePreflight(
        sessions, agents, lambda: clock[0], minimum_free_bytes=0
    ).ensure(
        document=revision.document,
        nodes={node_id: False},
        phase_index=0,
        request_key=str(uuid.uuid4()),
        actor="admin",
        previous=None,
    )
    assert checkpoint.pending_job_id is not None
    load_preflight = claim()
    assert load_preflight is not None
    assert load_preflight.operation == WireOperation.RUNTIME_PREFLIGHT
    assert isinstance(load_preflight.payload, RuntimePreflightRequest)
    agents.succeed(
        load_preflight,
        preflight_result(load_preflight.payload, observed_at=int(clock[0].timestamp())),
    )

    if not capacity_returns:
        clock[0] = deadline + timedelta(seconds=1)
        assert claim() is None
        with sessions() as session:
            expired = session.get(AgentOperation, order.id)
            assert expired is not None
            assert expired.current_attempt == 1
            assert expired.state != LifecycleState.BACKOFF
        return

    clock[0] += timedelta(seconds=3)
    inventory(1024**4)
    second = claim()
    assert second is not None and second.payload == first.payload
    with sessions() as session:
        assert (
            outstanding_disk_reservation_bytes(
                session, node_id, inventory_observed_at=clock[0]
            )
            > 0
        )
