"""Run/Switch operations: container phase."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.contracts import canonical_message
from vonk_agent_protocol.state_machines import ReservationState
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import CatalogDocumentRevision, RecipeSourceBundle
from vonk_control.models.execution import (
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.telemetry import NodeInventorySnapshot
from vonk_control.recipe_builds.common import RecipeBuildPlan
from vonk_control.recipe_operations.interfaces import RecipeOperationView
from vonk_control.recipe_operations.service import RecipeOperationService
from vonk_control.run_switch_contract.phase_results import (
    RunSwitchContainerBuildResult,
)
from vonk_control.run_switch_contract.plans import RunSwitchPlan
from vonk_control.run_switch_contract.requests import (
    RunSwitchApplyRequest,
    RunSwitchPreviewRequest,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations.executor import RecipeLifecyclePhaseExecutor
from vonk_control.run_switch_operations.result_helpers import _phase_result
from vonk_control.run_switch_operations.service import RunSwitchOperationService

from .recipe_operations_support import NOW, installed_recipe, setup_services
from .run_switch_operations_support import (
    PendingBuilds,
    RecordingArtifactExecutor,
    SynchronousPhaseExecutor,
    _request,
    _service,
)
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    inventory_items_fixture,
    set_inventory_identity,
)


@pytest.mark.usefixtures("damaged_json_rows")
def test_uncached_run_selects_external_fresh_builder_and_plans_container_phase(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, build_id, nodes = setup_services(tmp_path)
    source_digest = "c" * 64
    builder_id = "spk_" + "9" * 32
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        template_plan = dict(build.plan)
        template_policy_report = dict(build.policy_report)
        session.delete(build)
        session.add(
            RecipeSourceBundle(
                sha256=source_digest,
                media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                archive_bytes=1,
                total_bytes=1,
                file_count=1,
                storage_key="source-bundle-c",
                manifest={"schema_version": 1},
                verified_at=NOW,
            )
        )
        session.add(
            AgentNode(
                node_id=builder_id,
                state="active",
                inventory_snapshot=inventory_items_fixture(
                    builder_id,
                    observed_at=None,
                    identity=AgentRuntimeIdentity.model_validate(
                        {
                            **PACKAGED_RUNTIME_IDENTITY,
                            "architecture": "linux-arm64",
                            "binary_digest": "a" * 64,
                        }
                    ),
                ),
            )
        )
    InventoryRepository(sessions, clock=lambda: NOW).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": builder_id,
                    "observed_at": NOW,
                    "disk_total_bytes": 10_000,
                    "disk_free_bytes": 8_000,
                    "host_memory_total_bytes": 10_000,
                    "host_memory_free_bytes": 8_000,
                    "gpu_memory_total_bytes": 10_000,
                    "gpu_memory_free_bytes": 8_000,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": ("recipe.build.v1",),
                    "memory_pool": "shared",
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                }
            )
        )
    )
    with sessions.begin() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        assert revision is not None
        fake_builds = PendingBuilds(
            sessions,
            build_id=str(uuid.uuid4()),
            builder_node_id=builder_id,
            revision_id=revision.id,
            source_digest=source_digest,
            template_plan=template_plan,
            template_policy_report=template_policy_report,
        )
    lifecycle._builds = fake_builds
    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")

    assert plan.allowed is True
    assert plan.build.state == "planned"
    assert plan.build.builder_node_id == builder_id
    assert plan.build.build_input_sha256 == "d" * 64
    assert fake_builds.calls == [builder_id]
    assert [(phase.kind, phase.subphase) for phase in plan.phases[:4]] == [
        ("prepare", "container-build"),
        ("prepare", "runtime-image"),
        ("prepare", "runtime-plan"),
        ("transfer", "target-copy"),
    ]


@pytest.mark.usefixtures("damaged_json_rows")
def test_container_phase_delegates_to_existing_recipe_build_child(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, build_id, nodes = setup_services(tmp_path)
    source_digest = "c" * 64
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        build.state = "planned"
        build.image_digest = None
        build.oci_layout_sha256 = None
        build.image_bytes = None
        build.source_bundle_sha256 = source_digest
        session.add(
            RecipeSourceBundle(
                sha256=source_digest,
                media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                archive_bytes=1,
                total_bytes=1,
                file_count=1,
                storage_key="source-bundle-build",
                manifest={"schema_version": 1},
                verified_at=NOW,
            )
        )
        revision = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert revision is not None
        build.plan = {**build.plan, "source_bundle_sha256": source_digest}
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        set_inventory_identity(node, binary_digest="a" * 64)
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == nodes[0]
            )
        )
        assert snapshot is not None
        snapshot.capabilities = ["recipe.build.v1", "recipe.image.pull.v1"]

    class _LifecycleStub(RecipeOperationService):
        """Only ``build`` is reached by the container-build subphase."""

        def __init__(self) -> None:
            pass

    lifecycle_stub = _LifecycleStub()
    child_id = str(uuid.uuid4())
    build_plan = RecipeBuildPlan(
        build_id=build_id,
        recipe_revision_id=revision.id,
        recipe_content_sha256=revision.content_digest,
        builder_node_id=nodes[0],
        source_bundle_sha256=source_digest,
        build_input_sha256="e" * 64,
        agent_payload={"platform": "linux/arm64"},
    )
    with sessions.begin() as session:
        row = session.get(RecipeBuild, build_id)
        assert row is not None
        row.build_input_sha256 = build_plan.build_input_sha256
        row.plan = {
            **row.plan,
            "build_input_sha256": build_plan.build_input_sha256,
        }

    def start_build(*_args, **_kwargs) -> RecipeOperationView:
        with sessions.begin() as session:
            _kwargs["admission_guard"](session)
            build = session.get(RecipeBuild, build_id)
            assert build is not None
            build.state = "building"
        return RecipeOperationView(
            id=child_id,
            kind="recipe.build.v1",
            owner_id=build_id,
            state="running",
            plan_digest=build_plan.build_input_sha256,
            nodes=(),
            lifecycle_result=None,
        )

    lifecycle_stub.build = start_build
    executor = RecipeLifecyclePhaseExecutor(
        lifecycle_stub,
        sessions,
        # Mapping is not touched by the container subphase.
        ClusterMappingService(sessions),
        lambda: NOW,
    )
    request_key = str(uuid.uuid4())
    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    parent = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            request_key=request_key,
            plan_digest=plan.plan_digest,
        ),
        actor="admin",
    )
    assert parent.result is not None
    progress = parent.result
    phase = next(phase for phase in plan.phases if phase.subphase == "container-build")
    execution = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="admin",
        request_key=request_key,
        progress=progress,
    )
    assert execution.operation_id == child_id
    assert isinstance(execution.result, RunSwitchContainerBuildResult)
    assert execution.result.build_id == build_id
    assert execution.result.build_input_sha256 == "e" * 64
    assert execution.result.state == "building"
    assert execution.result.phase == "prepare"
    assert execution.result.subphase == "container-build"
    assert _phase_result(execution.result, phase=phase) == execution.result

    # A build preview superseded in place re-plans; it does not end the load.
    from vonk_agent_protocol.lifecycle_vocabulary import (
        InvalidRequestReason,
        WaitReason,
    )
    from vonk_control.recipe_operations.errors import RecipeRequestInvalid
    from vonk_control.run_switch_operations.errors import RunSwitchRetryLater

    def superseded_build(*_args, **_kwargs) -> RecipeOperationView:
        raise RecipeRequestInvalid(
            "recipe build preview is stale", reason=InvalidRequestReason.SUPERSEDED
        )

    lifecycle_stub.build = superseded_build
    with pytest.raises(RunSwitchRetryLater) as replan:
        executor.execute(
            plan,
            phase,
            item_index=0,
            actor="admin",
            request_key=request_key,
            progress=progress,
        )
    assert replan.value.typed_reason is WaitReason.SCOPE_CHANGED

    # A durable plan mutation is rejected before dispatch; execution never
    # re-plans around the changed identity.
    with sessions.begin() as session:
        row = session.get(RecipeBuild, build_id)
        assert row is not None
        row.plan = {**row.plan, "build_input_sha256": "a" * 64}
    with pytest.raises(Exception) as _ending:
        executor.execute(
            plan,
            phase,
            item_index=0,
            actor="admin",
            request_key=request_key,
            progress=progress,
        )


def test_a_refusal_that_survives_a_replan_retries_in_place_and_keeps_counting(
    tmp_path: Path,
) -> None:
    """A fresh plan that meets the same refusal again must not loop silently.

    Re-planning forgets the attempt count, so a refusal that planning cannot
    remove used to cycle fail, re-plan, fail forever and never reached the
    retry count that surfaces a named stall on the profile and in Fleet.
    """

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    now = [NOW]
    service = _service(
        sessions, NOW, lifecycle, RecordingArtifactExecutor(), phase_executor=None
    )
    service._clock = lambda: now[0]
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    assert plan.allowed
    parent = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            request_key=str(uuid.uuid4()),
            plan_digest=plan.plan_digest,
        ),
        actor="admin",
    )
    reason = "RuntimeError: current recipe is not authorized for OCI build receipt"

    def refuse() -> dict[str, object]:
        service._fail(
            parent.operation_id,
            reason,
            replan=True,
            checkpoint=(0, 0, None),
        )
        with sessions() as session:
            job = session.get(Job, parent.operation_id)
            assert job is not None and isinstance(job.result, dict)
            return dict(job.result)

    first = refuse()
    assert first["force_replan"] is True
    assert first["retry_attempt"] == 2
    first_due = service.get(parent.operation_id).next_attempt_at
    assert first_due is not None
    now[0] = first_due + timedelta(seconds=1)
    assert service._refresh_blocked_plan(parent.operation_id, now[0]) is True

    # The same refusal on the fresh plan is retried in place, and counted.
    second = refuse()
    assert second["force_replan"] is False
    assert second["retry_attempt"] == 2
    third = refuse()
    assert third["force_replan"] is False
    assert third["retry_attempt"] == 3


def test_exact_stop_reservation_budget_needs_a_fresh_post_stop_check(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    installation_operation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    installation_id = installation_operation.owner_id
    run_plan = lifecycle._run_admission.plan_run(
        installation_id,
        "old",
        now=lifecycle._clock(),
    )
    run_id = lifecycle._run_admission.accept_run(
        run_plan,
        actor="admin",
        now=lifecycle._clock(),
    )
    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        run = session.get(RecipeRun, run_id)
        assert installation is not None and run is not None
        installation.state = "partial"
        run.state = "running"
        run.route_state = "published"
        run_generation = run.run_generation
        for item in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            item.state = "running"
            item.reserved_memory_bytes = 7_800
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind == "unified-memory",
            )
        ):
            reservation.amount_bytes = 7_800

    request = _request(sessions, node_id, action="switch")
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    baseline = service.preview(request, actor="admin")
    required = baseline.fit_current.nodes[0].memory_required_bytes
    floor = baseline.fit_current.nodes[0].memory_floor_bytes
    assert required is not None and floor is not None
    # Aggregate free already accounts for the running owner. The declared
    # peak still owns the hard budget until this exact run stops.
    total = 7_800 + required + floor - 1
    free = required + floor + 10
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.host_memory_total_bytes = total
        snapshot.host_memory_free_bytes = free
        snapshot.gpu_memory_total_bytes = total
        snapshot.gpu_memory_free_bytes = free
    plan = service.preview(request, actor="admin")

    assert plan.fit_current.allowed is False
    assert plan.fit_after_stop is None
    assert plan.post_stop_memory_check is not None
    assert plan.post_stop_memory_check.stop_run_ids == [run_id]
    assert plan.allowed, plan.blockers
    assert plan.stop_before_prepare is True
    current_node = plan.fit_current.nodes[0]
    uncertainty = current_node.memory_usage_uncertainty
    assert uncertainty is not None
    sample = next(
        item for item in plan.freshness if item.source == f"spark:{node_id}:inventory"
    )
    assert uncertainty.source == "aggregate_inventory_without_run_usage"
    assert uncertainty.inventory_observed_at == sample.observed_at
    assert uncertainty.inventory_evidence_digest == sample.evidence_digest
    assert [
        (
            item.run_id,
            item.run_generation,
            item.reservation_kind,
            item.maximum_bytes,
        )
        for item in uncertainty.residual_ranges
    ] == [(run_id, run_generation, "unified-memory", 7_800)]
    assert current_node.memory_free_after_bytes is None
    round_tripped = RunSwitchPlan.model_validate_json(plan.model_dump_json())
    assert round_tripped.fit_current.nodes[0].memory_usage_uncertainty == uncertainty
    bad_stop = plan.model_dump(mode="python")
    bad_stop["post_stop_memory_check"] = {"stop_run_ids": [str(uuid.uuid4())]}
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        RunSwitchPlan.model_validate(bad_stop)
    # No Spark build needs the old workload's memory, so it keeps serving
    # through preparation and stops only right before start.
    assert [phase.kind for phase in plan.phases] == [
        "prepare",
        "prepare",
        "stop",
        "start",
        "final_verify",
    ]
    required = plan.fit_current.nodes[0].memory_required_bytes
    floor = plan.fit_current.nodes[0].memory_floor_bytes
    assert required is not None and floor is not None
    impossible_total = required + floor - 1
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        saved_capacity = (
            snapshot.host_memory_total_bytes,
            snapshot.host_memory_free_bytes,
            snapshot.gpu_memory_total_bytes,
            snapshot.gpu_memory_free_bytes,
        )
        snapshot.host_memory_total_bytes = impossible_total
        snapshot.host_memory_free_bytes = impossible_total
        snapshot.gpu_memory_total_bytes = impossible_total
        snapshot.gpu_memory_free_bytes = impossible_total
    physically_impossible = service.preview(request, actor="admin")
    # An estimate above the Spark is tried after the stop, with the claim cut
    # to the Spark less its floor; the stop is covered by the check.
    assert physically_impossible.post_stop_memory_check is not None
    assert physically_impossible.allowed
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        (
            snapshot.host_memory_total_bytes,
            snapshot.host_memory_free_bytes,
            snapshot.gpu_memory_total_bytes,
            snapshot.gpu_memory_free_bytes,
        ) = saved_capacity
    with sessions.begin() as session:
        claim = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind == "unified-memory",
                ResourceReservation.state == "active",
            )
        )
        assert claim is not None
        claim.plan_digest = "a" * 64
    changed = service.preview(request, actor="admin")
    assert changed.post_stop_memory_check is None
    assert not changed.allowed


def _single_run_replaced_by_a_dual(
    tmp_path: Path,
    *,
    resident_on_stopped_spark: bool = False,
) -> tuple[
    sessionmaker[Session],
    RunSwitchOperationService,
    RunSwitchPreviewRequest,
    str,
    tuple[str, ...],
]:
    """One run serving on the first of two Sparks, and a switch to a dual.

    Each Spark's pool fits the dual's demand, but not that demand and the run's
    peak: on the first Spark only the run's planned stop makes room. With
    ``resident_on_stopped_spark`` the first Spark's observed free memory still
    holds the running workload, as a live Spark reports it before the stop.
    """
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation_id = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    ).owner_id
    run_id = lifecycle._run_admission.accept_run(
        lifecycle._run_admission.plan_run(
            installation_id, "old", now=lifecycle._clock()
        ),
        actor="admin",
        now=lifecycle._clock(),
    )
    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        run = session.get(RecipeRun, run_id)
        assert installation is not None and run is not None
        installation.state = "partial"
        run.state = "running"
        run.route_state = "published"
        group = SparkGroup(
            nodes=[
                SparkGroupNode(
                    node_id=item.node_id,
                    rank=item.rank,
                    role=item.role,
                    endpoint_owner=item.rank == 0,
                )
                for item in session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            ]
        )
        assert [item.node_id for item in group.nodes] == list(nodes)
        # The outgoing run serves on the first Spark only.
        for item in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            if item.node_id == nodes[1]:
                session.delete(item)
            else:
                item.state = "running"
                item.reserved_memory_bytes = 7_800
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
            )
        ):
            if reservation.node_id == nodes[1]:
                session.delete(reservation)
            elif reservation.kind == "unified-memory":
                reservation.amount_bytes = 7_800

    request = _request(sessions, nodes[0], action="switch").model_copy(
        update={"spark_group": group}
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    baseline = service.preview(request, actor="admin")
    required = baseline.fit_current.nodes[0].memory_required_bytes
    floor = baseline.fit_current.nodes[0].memory_floor_bytes
    assert required is not None and floor is not None
    with sessions.begin() as session:
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.host_memory_total_bytes = 7_800 + required + floor - 1
            snapshot.gpu_memory_total_bytes = 7_800 + required + floor - 1
            free = (
                floor
                if resident_on_stopped_spark and snapshot.node_id == nodes[0]
                else required + floor + 10
            )
            snapshot.host_memory_free_bytes = free
            snapshot.gpu_memory_free_bytes = free
    return sessions, service, request, run_id, nodes


def _independent_build(sessions: sessionmaker[Session], node_id: str) -> str:
    """A build no stop releases, holding its claims as the builder records them."""
    build_id = str(uuid.uuid4())
    build_input = hashlib.sha256(build_id.encode()).hexdigest()
    with sessions.begin() as session:
        session.add_all(
            ResourceReservation(
                node_id=node_id,
                kind=kind,
                resource_key=build_input,
                amount_bytes=amount,
                owner_kind="recipe-build",
                owner_id=build_id,
                state=ReservationState.ACTIVE,
                plan_digest=build_input,
                created_at=NOW,
            )
            for kind, amount in (("disk", 1), ("host-memory", 7_800))
        )
    return build_id


@pytest.mark.parametrize("resident_on_stopped_spark", [False, True])
def test_a_switch_waiting_on_another_claim_names_it_not_the_planned_stop(
    tmp_path: Path, resident_on_stopped_spark: bool
) -> None:
    """The stop-bound shortfall on the stopped run's Spark is cleared by its stop.

    The switch still waits, but on the claim no stop releases, and that is what
    its blockers (and so the wait's headline) name.
    """
    sessions, service, request, run_id, nodes = _single_run_replaced_by_a_dual(
        tmp_path, resident_on_stopped_spark=resident_on_stopped_spark
    )
    # Only the stop stands between the dual and its start: it is admitted
    # behind a fresh post-stop check.
    clear = service.preview(request, actor="admin")
    assert clear.post_stop_memory_check is not None
    assert clear.post_stop_memory_check.stop_run_ids == [run_id]
    assert clear.allowed, clear.blockers

    holder = _independent_build(sessions, nodes[1])
    waiting = service.preview(request, actor="admin")

    assert not waiting.allowed
    assert waiting.post_stop_memory_check is None
    # The wait's headline is the claim no stop releases, on its Spark.
    assert waiting.blockers
    assert waiting.blockers[0].node_ids == [nodes[1]]
    assert holder in waiting.blockers[0].detail, [
        (reason.code, reason.node_ids, reason.detail) for reason in waiting.blockers
    ]


def test_a_claim_beside_the_stopped_run_stays_part_of_the_wait(
    tmp_path: Path,
) -> None:
    """A claim on the stopped run's Spark that the stop does not release is named.

    Releasing the other Spark's claim alone would still leave the switch waiting
    on it, so the wait names both claims, the other Spark's first.
    """
    sessions, service, request, _run_id, nodes = _single_run_replaced_by_a_dual(
        tmp_path
    )
    beside_stop = _independent_build(sessions, nodes[0])
    other_spark = _independent_build(sessions, nodes[1])
    waiting = service.preview(request, actor="admin")

    assert not waiting.allowed
    assert waiting.post_stop_memory_check is None
    assert other_spark in waiting.blockers[0].detail
    assert any(
        reason.node_ids == [nodes[0]] and beside_stop in reason.detail
        for reason in waiting.blockers
    ), [(reason.code, reason.node_ids, reason.detail) for reason in waiting.blockers]


def test_recheck_binds_the_inventory_sample_it_reads_not_the_reviewed_one(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    installation_operation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    installation_id = installation_operation.owner_id
    run_plan = lifecycle._run_admission.plan_run(
        installation_id,
        "old",
        now=lifecycle._clock(),
    )
    run_id = lifecycle._run_admission.accept_run(
        run_plan,
        actor="admin",
        now=lifecycle._clock(),
    )
    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        run = session.get(RecipeRun, run_id)
        assert installation is not None and run is not None
        installation.state = "partial"
        run.state = "running"
        run.route_state = "published"
        for item in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            item.state = "running"
            item.reserved_memory_bytes = 7_800
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind == "unified-memory",
            )
        ):
            reservation.amount_bytes = 7_800

    request = _request(sessions, node_id, action="switch")
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    baseline = service.preview(request, actor="admin")
    required = baseline.fit_current.nodes[0].memory_required_bytes
    floor = baseline.fit_current.nodes[0].memory_floor_bytes
    assert required is not None and floor is not None
    # Aggregate free already accounts for the running owner. The declared
    # peak still owns the hard budget until this exact run stops.
    total = 7_800 + required + floor - 1
    free = required + floor + 10
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.host_memory_total_bytes = total
        snapshot.host_memory_free_bytes = free
        snapshot.gpu_memory_total_bytes = total
        snapshot.gpu_memory_free_bytes = free
    plan = service.preview(request, actor="admin")

    assert plan.fit_current.allowed is False
    assert plan.post_stop_memory_check is not None
    reviewed_sample = next(
        item for item in plan.freshness if item.source == f"spark:{node_id}:inventory"
    )

    # The Spark reports again after the review. A parked profile admission is
    # retried minutes later, so this is the normal case, not a race.
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.observed_at = snapshot.observed_at + timedelta(seconds=1)
    with sessions() as session:
        rechecked = service.recheck_resources_in_session(session, request, plan)

    assert rechecked.allowed
    sample = next(
        item
        for item in rechecked.freshness
        if item.source == f"spark:{node_id}:inventory"
    )
    assert reviewed_sample.observed_at is not None
    assert sample.observed_at == reviewed_sample.observed_at + timedelta(seconds=1)
    uncertainty = rechecked.fit_current.nodes[0].memory_usage_uncertainty
    assert uncertainty is not None
    assert uncertainty.inventory_observed_at == sample.observed_at


def test_switch_preserves_the_memory_floor_on_an_idle_spark(
    tmp_path: Path,
) -> None:
    """An idle Spark still needs host headroom; fresh capacity admits a new request."""
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    request = _request(sessions, node_id, action="switch")
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    baseline = service.preview(request, actor="admin")
    required = baseline.fit_current.nodes[0].memory_required_bytes
    floor = baseline.fit_current.nodes[0].memory_floor_bytes
    assert required is not None and floor is not None
    for headroom in (-1, 0):
        total = required + floor + headroom
        with sessions.begin() as session:
            snapshot = session.scalar(select(NodeInventorySnapshot))
            assert snapshot is not None
            snapshot.host_memory_total_bytes = total
            snapshot.host_memory_free_bytes = total
            snapshot.gpu_memory_total_bytes = total
            snapshot.gpu_memory_free_bytes = total
        plan = service.preview(request, actor="admin")
        # A declared peak is an estimate: one byte short of the floor is
        # tried, claiming the Spark less its floor, with a warning.
        assert plan.fit_current.allowed
        assert plan.allowed
        node = plan.fit_current.nodes[0]
        assert node.memory_required_bytes == min(required, total - floor)
        assert any("only claim" in warning.detail for warning in plan.warnings) == (
            headroom != 0
        )


def test_switch_tries_an_estimate_above_an_idle_spark_as_its_only_claim(
    tmp_path: Path,
) -> None:
    """A declared peak is an estimate: one byte over the idle maximum is tried."""
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    request = _request(sessions, node_id, action="switch")
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    baseline = service.preview(request, actor="admin")
    required = baseline.fit_current.nodes[0].memory_required_bytes
    floor = baseline.fit_current.nodes[0].memory_floor_bytes
    assert required is not None and floor is not None
    total = required + floor - 1
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.host_memory_total_bytes = total
        snapshot.host_memory_free_bytes = total
        snapshot.gpu_memory_total_bytes = total
        snapshot.gpu_memory_free_bytes = total
    plan = service.preview(request, actor="admin")
    assert plan.allowed
    node = plan.fit_current.nodes[0]
    # The try claims the Spark less its floor (total - floor): the estimate cut
    # to what the Spark can hold, leaving the floor.
    assert node.memory_required_bytes == total - floor
    assert node.memory_free_after_bytes == floor


def test_switch_replaces_the_run_that_holds_the_nodes_capacity(
    tmp_path: Path,
) -> None:
    """The run a plan stops must not block that same plan.

    Wrong implementation this catches: the low-level run admission summed every
    active reservation, so an installed replacement reported
    ``run.insufficient_memory`` and ``run-switch.run_admission_blocked`` for the
    memory and ports the stopped run itself held.  Because the only release
    path is a successful stop, no reviewed plan could ever stop that run; live,
    that wedged a GLM load behind a 122 GB unified-memory reservation whose run
    was no longer running.
    """
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    installation_operation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    installation_id = installation_operation.owner_id
    run_plan = lifecycle._run_admission.plan_run(
        installation_id,
        "old",
        now=lifecycle._clock(),
    )
    run_id = lifecycle._run_admission.accept_run(
        run_plan,
        actor="admin",
        now=lifecycle._clock(),
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = "running"
        run.route_state = "published"
        # The live run holds nearly all of the node's memory, so a replacement
        # can only be admitted once the plan's own Stop phase frees it.
        for item in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            item.state = "running"
            item.reserved_memory_bytes = 7_900
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind == "unified-memory",
            )
        ):
            reservation.amount_bytes = 7_900

    request = _request(sessions, node_id, action="switch")
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    plan = service.preview(request, actor="admin")

    codes = {reason.code for reason in plan.blockers}
    assert "run.port_occupied" not in codes
    assert "run.rendezvous_port_occupied" not in codes
    assert "run-switch.run_admission_blocked" not in codes
    assert plan.allowed is True
    assert [stop.run_id for stop in plan.stops] == [run_id]


def test_switch_replaces_a_running_model_with_an_estimate_above_the_idle_maximum(
    tmp_path: Path,
) -> None:
    """A switch to an over-max estimate stops the old run first, then tries.

    Catches the replaced run counting as a holder that keeps the new estimate
    waiting, while the post-stop check says no stop can ever fit it: the switch
    then waited on its own stop for ever.
    """
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    installation_operation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    installation_id = installation_operation.owner_id
    run_plan = lifecycle._run_admission.plan_run(
        installation_id, "old", now=lifecycle._clock()
    )
    estimate = run_plan.nodes[0].required_memory_bytes
    floor = run_plan.nodes[0].memory_floor_bytes
    total = estimate + floor - 1
    run_id = lifecycle._run_admission.accept_run(
        run_plan, actor="admin", now=lifecycle._clock()
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = "running"
        run.route_state = "published"
        for item in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            item.state = "running"
            item.reserved_memory_bytes = total - floor
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind == "unified-memory",
            )
        ):
            reservation.amount_bytes = total - floor
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        # The old run uses all but the floor; the Spark is one byte short of
        # what the estimate and the floor ask.
        snapshot.host_memory_total_bytes = total
        snapshot.gpu_memory_total_bytes = total
        snapshot.host_memory_free_bytes = floor
        snapshot.gpu_memory_free_bytes = floor

    request = _request(sessions, node_id, action="switch")
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    plan = service.preview(request, actor="admin")

    assert plan.allowed is True, plan.blockers
    assert plan.post_stop_memory_check is not None
    assert [stop.run_id for stop in plan.stops] == [run_id]
    assert plan.fit_current.nodes[0].memory_required_bytes == total - floor
