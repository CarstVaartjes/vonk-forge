from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx2
import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    LifecycleState,
    ReservationState,
    canonical_message,
)
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.install_admission import (
    AdmissionReason,
    installation_plan_digest_from_stored_document,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    CatalogDocumentRevision,
    InstallationNode,
    Job,
    NodeInventorySnapshot,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.operation_api import OperationQuery
from vonk_control.operations.blockers import PHASE_RETRY_CODE
from vonk_control.operations.item_contract import operation_item
from vonk_control.run_admission import RunAdmissionBusy
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchCleanupApplyRequest,
    RunSwitchCleanupPreviewRequest,
    RunSwitchCleanupVerifyResult,
    RunSwitchFinalVerifyResult,
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchUninstallResult,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import (
    RecipeLifecyclePhaseExecutor,
    RunSwitchIssuedWorkloadPending,
    RunSwitchOperationService,
    _node_missing_bytes,
    _planned_transfer_bytes,
)

from .recipe_operations_support import (
    NOW,
    installed_recipe,
    setup_services,
    started_recipe,
)
from .run_switch_operations_support import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    SynchronousPhaseExecutor,
    _child_operation_id,
    _ObservingLifecycle,
    _parked_start_switch,
    _request,
    _result,
    _service,
)


def _admission_delay(reason: str | None, attempt: int) -> int:
    """The delay a capacity wait names: the lifecycle core's backoff (2, 4, 8 ...)."""

    found = re.search(rf"admission retry {attempt} in (\d+)s", reason or "")
    assert found is not None, reason
    return int(found.group(1))


def _make_install_specs_missing_placement_authority(
    sessions,
    installation_id: str,
    nodes: tuple[str, ...],
) -> None:
    """Represent an exact old successful install with an invalid launch schema."""

    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None
        stored_plan = json.loads(json.dumps(installation.plan))
        install_job = session.scalar(
            select(Job).where(
                Job.kind == "recipe.install",
                Job.payload["owner_id"].as_string() == installation_id,
            )
        )
        assert install_job is not None and install_job.result is not None
        operations = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == install_job.id)
                .order_by(AgentOperation.node_id)
            )
        )
        assert {operation.node_id for operation in operations} == set(nodes)
        for index, operation in enumerate(operations):
            payload = json.loads(json.dumps(operation.payload))
            compiled = payload["compiled_execution_plan"]
            placement = compiled["runtime"]["placement"]
            assert "memory_floor_bytes" in placement
            placement.pop("memory_floor_bytes")
            payload["compiled_execution_plan"] = compiled
            operation.payload = payload
            operation.payload_digest = hashlib.sha256(
                canonical_message(payload)
            ).hexdigest()
            operation.current_attempt = 1
            evidence = install_job.result["node_evidence"][operation.node_id]
            session.add(
                AgentOperationAttempt(
                    operation_id=operation.id,
                    attempt=1,
                    fence=str(uuid.uuid4()),
                    lease_deadline=NOW + timedelta(minutes=1),
                    agent_certificate_serial=f"serial-{index}",
                    state="succeeded",
                    result=evidence,
                )
            )
            stored_plan["compiled_execution_plans"][operation.node_id] = compiled
        plan_digest = installation_plan_digest_from_stored_document(stored_plan)
        stored_plan["plan_digest"] = plan_digest
        installation.plan_digest = plan_digest
        job_payload = json.loads(json.dumps(install_job.payload))
        job_payload["plan_digest"] = plan_digest
        install_job.payload = job_payload
        install_job.payload_digest = hashlib.sha256(
            canonical_message(job_payload)
        ).hexdigest()
        for operation in operations:
            payload = json.loads(json.dumps(operation.payload))
            payload["plan_digest"] = plan_digest
            operation.payload = payload
            operation.payload_digest = hashlib.sha256(
                canonical_message(payload)
            ).hexdigest()
        installation.plan = stored_plan


def _record_successful_reconcile_member(
    sessions, lifecycle, job_id: str, node_id: str
) -> dict[str, object]:
    with sessions.begin() as session:
        child = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == job_id,
                AgentOperation.node_id == node_id,
            )
        )
        assert child is not None
        child.current_attempt = 1
        session.add(
            AgentOperationAttempt(
                operation_id=child.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                lease_deadline=NOW + timedelta(minutes=1),
                agent_certificate_serial="serial-0",
                state="succeeded",
                result={},
            )
        )
    lifecycle.record_node_result(job_id, node_id, succeeded=True, evidence={})
    return {}


@pytest.mark.usefixtures("damaged_json_rows")
def test_reconcile_review_binds_opaque_invalid_launch_spec_and_uninstall_rebuilds_its_ranks(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    _make_install_specs_missing_placement_authority(
        sessions, installation.owner_id, nodes
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )

    plan = service.preview_cleanup(
        RunSwitchCleanupPreviewRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
        ),
        actor="admin",
    )

    assert plan.allowed, [(reason.code, reason.detail) for reason in plan.blockers]
    assert plan.cleanup_mode == "reconcile"
    assert plan.reconciliation_authority is not None
    assert [target.node_id for target in plan.reconciliation_authority.targets] == list(
        nodes
    )
    assert all(
        target.state == "pending" for target in plan.reconciliation_authority.targets
    )
    # The ordinary uninstall no longer refuses on a damaged launch document: its
    # ranks are rebuilt from the saved mapping and the damage is retired as unknown.
    assert lifecycle.preview_uninstall(installation.owner_id).nodes
    with sessions() as session:
        assert (
            session.scalar(select(Job.id).where(Job.kind == "recipe.reconcile")) is None
        )


@pytest.mark.usefixtures("damaged_json_rows")
def test_reconcile_run_switch_releases_install_claims_after_group_cleanup(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    _make_install_specs_missing_placement_authority(
        sessions, installation.owner_id, nodes
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )
    preview = service.preview_cleanup(
        RunSwitchCleanupPreviewRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
        ),
        actor="admin",
    )
    assert preview.allowed, [
        (reason.code, reason.detail) for reason in preview.blockers
    ]

    operation = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert service.tick() is True
    child_id = _child_operation_id(service.get(operation.operation_id))
    assert child_id is not None
    with sessions.begin() as session:
        child_job = session.get(Job, child_id)
        assert child_job is not None and child_job.kind == "recipe.reconcile"
        child_operations = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == child_job.id)
                .order_by(AgentOperation.node_id)
            )
        )
        assert [item.node_id for item in child_operations] == sorted(nodes)
        for index, child in enumerate(child_operations):
            child.current_attempt = 1
            session.add(
                AgentOperationAttempt(
                    operation_id=child.id,
                    attempt=1,
                    fence=str(uuid.uuid4()),
                    lease_deadline=NOW + timedelta(minutes=1),
                    agent_certificate_serial=f"serial-{index}",
                    state="succeeded",
                    result={},
                )
            )

    lifecycle.record_node_result(child_id, nodes[0], succeeded=True, evidence={})
    with sessions() as session:
        held = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == installation.owner_id
                )
            )
        )
        assert held and all(item.state == "active" for item in held)

    lifecycle.record_node_result(child_id, nodes[1], succeeded=True, evidence={})
    with sessions() as session:
        row = session.get(RecipeInstallation, installation.owner_id)
        assert row is not None and row.state == "uninstalled"
        released = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == installation.owner_id
                )
            )
        )
        assert released and all(item.state == "released" for item in released)

    for _ in range(6):
        if service.get(operation.operation_id).state not in {"queued", "running"}:
            break
        service.tick()
    completed = service.get(operation.operation_id)
    assert completed.state == "succeeded", completed.status_reason
    cleanup_result = next(
        item
        for item in _result(completed).phase_results
        if isinstance(item, RunSwitchCleanupVerifyResult)
    )
    assert cleanup_result.cleanup_mode == "reconcile"
    assert cleanup_result.final_verified is True


@pytest.mark.usefixtures("damaged_json_rows")
def test_new_reconcile_review_reuses_partial_cleanup_and_releases_last_claim(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    _make_install_specs_missing_placement_authority(
        sessions, installation.owner_id, nodes
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )
    first_plan = service.preview_cleanup(
        RunSwitchCleanupPreviewRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
        ),
        actor="admin",
    )
    assert first_plan.allowed, [
        (item.code, item.detail) for item in first_plan.blockers
    ]
    first = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert service.tick() is True
    first_child_id = _child_operation_id(service.get(first.operation_id))
    assert first_child_id is not None

    _record_successful_reconcile_member(sessions, lifecycle, first_child_id, nodes[0])
    lifecycle.record_node_result(
        first_child_id,
        nodes[1],
        succeeded=False,
        evidence={
            "failure_kind": "temporary-dependency",
            "reason": "temporary dependency",
        },
    )
    from vonk_control.run_switch_operations.constants import (
        _FINAL_VERIFICATION_MAX_SECONDS,
    )

    service._clock = lambda: (
        lifecycle._clock() + timedelta(seconds=_FINAL_VERIFICATION_MAX_SECONDS + 1)
    )
    for _ in range(6):
        if service.get(first.operation_id).state not in {"queued", "running"}:
            break
        service.tick()
    assert service.get(first.operation_id).state == "failed"
    with sessions() as session:
        partial = session.get(RecipeInstallation, installation.owner_id)
        assert partial is not None and partial.state == "partial"
        members = tuple(
            session.scalars(
                select(InstallationNode)
                .where(InstallationNode.installation_id == installation.owner_id)
                .order_by(InstallationNode.rank)
            )
        )
        assert [(node.node_id, node.state) for node in members] == [
            (nodes[0], "uninstalled"),
            (nodes[1], "failed"),
        ]

    # The partial result must admit a fresh cleanup, reuse the completed rank,
    # and release all claims once the remaining exact effect is confirmed.
    retry_plan = service.preview_cleanup(
        RunSwitchCleanupPreviewRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
        ),
        actor="admin",
    )
    assert retry_plan.allowed, [
        (item.code, item.detail) for item in retry_plan.blockers
    ]
    assert retry_plan.reconciliation_authority is not None
    assert [
        (target.node_id, target.state)
        for target in retry_plan.reconciliation_authority.targets
    ] == [(nodes[0], "reconciled"), (nodes[1], "pending")]
    retry = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert service.tick() is True
    retry_child_id = _child_operation_id(service.get(retry.operation_id))
    assert retry_child_id is not None
    assert retry.operation_id != first.operation_id
    assert retry_child_id != first_child_id
    with sessions() as session:
        retry_children = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == retry_child_id
                )
            )
        )
        assert [(child.node_id, child.kind) for child in retry_children] == [
            (nodes[1], "recipe.reconcile")
        ]
    _record_successful_reconcile_member(sessions, lifecycle, retry_child_id, nodes[1])
    with sessions() as session:
        completed = session.get(RecipeInstallation, installation.owner_id)
        assert completed is not None and completed.state == "uninstalled"
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == installation.owner_id
                )
            )
        )
        assert claims and all(
            item.state == ReservationState.RELEASED for item in claims
        )


def test_scoped_cleanup_removes_the_installation_through_run_switch(
    tmp_path: Path,
) -> None:
    """Run/Switch owns the removal, its child reference and its verification."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )
    operation = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation.owner_id,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert service.tick() is True
    view = service.get(operation.operation_id)
    assert view.state == "running"
    child_id = _child_operation_id(view)
    assert child_id is not None

    for node_id in nodes:
        lifecycle.record_node_result(
            child_id,
            node_id,
            succeeded=True,
            evidence={},
        )

    for _ in range(4):
        if service.get(operation.operation_id).state not in {"queued", "running"}:
            break
        service.tick()

    completed = service.get(operation.operation_id)
    assert completed.state == "succeeded", completed.status_reason
    assert "uninstall" in _result(completed).completed_phases
    with sessions() as session:
        row = session.get(RecipeInstallation, installation.owner_id)
        assert row is None or row.state == "uninstalled"


def test_scoped_cleanup_retries_when_uninstall_capacity_writer_is_busy(
    tmp_path: Path,
) -> None:
    """A busy uninstall admission parks the exact cleanup checkpoint."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )
    operation = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation.owner_id,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    original_uninstall = lifecycle.uninstall

    def busy_once(*args, **kwargs):
        del args, kwargs
        raise RunAdmissionBusy("run capacity writer is busy")

    lifecycle.uninstall = busy_once  # type: ignore[method-assign]
    try:
        assert service.tick() is True
    finally:
        lifecycle.uninstall = original_uninstall  # type: ignore[method-assign]

    parked = service.get(operation.operation_id)
    assert parked.state == "running"
    assert parked.status_reason is not None
    assert _admission_delay(parked.status_reason, 1) in range(1, 4)
    assert parked.result is not None
    # Recovery is asserted through effects and ownership below.
    assert parked.result.phase_index == 0
    assert parked.result.item_index == 0


def test_a_retryable_plan_blocker_is_named_not_hidden_as_capacity_busy(
    tmp_path: Path,
) -> None:
    """The phase retry carries the underlying blocker code and detail.

    Wrong implementation: every retryable plan blocker surfaced as
    ``run.capacity_busy``, so a dual load waited two hours behind a generic
    "capacity writer" with no hint that memory or a port was the cause.
    """

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )
    operation = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation.owner_id,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    blocker = AdmissionReason(
        "run.insufficient_memory", "spark-1 has 10 GiB free but needs 80 GiB"
    )

    def blocked(*args, **kwargs):
        del args, kwargs
        raise RunAdmissionBusy("blocked", code=blocker.code, blockers=(blocker,))

    original_uninstall = lifecycle.uninstall
    lifecycle.uninstall = blocked  # type: ignore[method-assign]
    try:
        assert service.tick() is True
    finally:
        lifecycle.uninstall = original_uninstall  # type: ignore[method-assign]

    parked = service.get(operation.operation_id)
    assert parked.result is not None
    assert parked.status_reason is not None
    retry = [b for b in parked.blockers if b.code == PHASE_RETRY_CODE]
    assert len(retry) == 1


def test_capacity_backoff_and_checkpoint_retry_share_one_attempt_counter(
    tmp_path: Path,
) -> None:
    """Capacity waits and checkpoint retries interleave on ``retry_attempt``.

    Each owner backs off from the shared counter, a capacity wait for a new
    reason starts its own backoff again, and phase progress clears it.
    """

    from vonk_control.run_switch_operations import _read_progress

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    now = [lifecycle._clock()]
    service = _service(sessions, now[0], lifecycle, RecordingArtifactExecutor())
    service._clock = lambda: now[0]
    operation = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation.owner_id,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    original_uninstall = lifecycle.uninstall

    def busy(*args, **kwargs):
        del args, kwargs
        raise RunAdmissionBusy("run capacity writer is busy")

    def result():
        view = service.get(operation.operation_id)
        assert view.result is not None
        return view, view.result

    def advance_to_due() -> None:
        view, _current = result()
        assert view.next_attempt_at is not None
        now[0] = view.next_attempt_at + timedelta(seconds=1)

    lifecycle.uninstall = busy  # type: ignore[method-assign]
    try:
        assert service.tick() is True
        view, current = result()
        assert _admission_delay(view.status_reason, 1) in range(1, 4)
        advance_to_due()
        assert service.tick() is True
        view, current = result()
        assert _admission_delay(view.status_reason, 2) in range(3, 6)
        assert current.retry_attempt == 3

        # A checkpoint retry continues the same counter, bounded by its cap.
        with sessions.begin() as session:
            job = session.get(Job, operation.operation_id, with_for_update=True)
            assert job is not None
            progress = _read_progress(job.result)
            RunSwitchOperationService._schedule_checkpoint_retry(
                job, progress, "artifact.temporarily-unavailable", now[0]
            )
        view, current = result()
        assert view.state == "running"
        assert current.retry_attempt == 4
        assert view.next_attempt_at is not None
        assert timedelta(0) < view.next_attempt_at - now[0] <= timedelta(seconds=75)

        # A capacity wait after a different reason starts its own backoff.
        advance_to_due()
        assert service.tick() is True
        view, current = result()
        assert _admission_delay(view.status_reason, 1) in range(1, 4)
        assert current.retry_attempt == 2
        # Recovery is asserted through effects and ownership below.
    finally:
        lifecycle.uninstall = original_uninstall  # type: ignore[method-assign]

    # Phase progress clears the shared counter.
    advance_to_due()
    assert service.tick() is True
    view, current = result()
    assert view.state == "running", view.status_reason
    assert current.retry_attempt is None
    assert current.phase_index == 0


def _planned_installation(tmp_path: Path, *, nodes: int = 2):
    """Admit a plan that is persisted but never launched on any node."""

    sessions, lifecycle, _queue, mapping_id, build_id, node_ids = setup_services(
        tmp_path, nodes=nodes
    )
    admission = lifecycle._install_admission
    plan = admission.plan_install(mapping_id, build_id, now=NOW)
    assert plan.allowed, [
        (node.node_id, reason.code) for node in plan.nodes for reason in node.blockers
    ]
    installation_id = admission.accept_install(plan, actor="admin", now=NOW)
    with sessions() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None and installation.state == "planned"
    return sessions, lifecycle, installation_id, node_ids


def test_scoped_cleanup_abandons_a_never_installed_plan(tmp_path: Path) -> None:
    """A persisted plan that never reached a node is abandoned, not removed.

    Uninstalling it would ask every Spark to remove bytes it never received.
    The installation's own assessment owns the disposition, so cleanup queues
    no agent work and the receipt still names why the record was disposed of.
    """

    sessions, lifecycle, installation_id, _nodes = _planned_installation(tmp_path)
    service = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
    )

    preview = service.preview_cleanup(installation_id, actor="admin")

    assert preview.allowed is True, [reason.code for reason in preview.blockers]
    assert preview.cleanup_disposition == "abandon"

    operation = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation_id, request_key=str(uuid.uuid4())
        ),
        actor="admin",
    )
    for _ in range(4):
        if service.get(operation.operation_id).state not in {"queued", "running"}:
            break
        service.tick()

    completed = service.get(operation.operation_id)
    assert completed.state == "succeeded", completed.status_reason
    receipt = next(
        item
        for item in _result(completed).phase_results
        if isinstance(item, RunSwitchUninstallResult)
    )
    assert receipt.disposition == "abandoned"
    # Recovery is asserted through effects and ownership below.
    # A restarted phase replays the disposal, so the effect is idempotent.
    replay = lifecycle.abandon_never_installed(installation_id)
    assert replay.installation_id == installation_id
    assert replay.disposition == receipt.disposition
    with sessions() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None and installation.state == "uninstalled"
        members = tuple(
            session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation_id
                )
            )
        )
        assert members
        assert all(node.state == "uninstalled" for node in members)
        # The admission reservation must not leak for an abandoned plan.
        assert all(
            reservation.state == "released"
            for reservation in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == installation_id
                )
            )
        )


def test_scoped_cleanup_refuses_a_planned_row_with_installed_bytes(
    tmp_path: Path,
) -> None:
    """A planned row that contradicts itself stays a reported blocker.

    Installed bytes on a never-launched plan are exactly the contradiction that
    must not be silently abandoned, so the uninstall assessment keeps refusing
    it under ``run-switch.uninstall-blocked``.
    """

    sessions, lifecycle, installation_id, _nodes = _planned_installation(tmp_path)
    with sessions.begin() as session:
        member = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation_id
            )
        )
        assert member is not None
        member.state = "planned"
        member.installed_bytes = 120
    service = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
    )

    preview = service.preview_cleanup(installation_id, actor="admin")

    assert preview.allowed is False
    assert preview.cleanup_disposition == "uninstall"
    operation = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation_id, request_key=str(uuid.uuid4())
        ),
        actor="admin",
    )
    assert operation.state in {"queued", LifecycleState.OBSERVING}
    with sessions() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None and installation.state == "planned"


def test_parked_switch_observes_recovered_child_after_controller_restart(
    tmp_path: Path,
) -> None:
    """A parked parent must not miss its child's later exact success."""
    service, operation, _ = _parked_start_switch(tmp_path, healthy=False)
    now = [NOW]
    service._clock = lambda: now[0]
    with service._sessions.begin() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        row.state = "observing"
    restarted = RunSwitchOperationService(
        service._sessions,
        lifecycle=service._lifecycle,
        clock=lambda: now[0],
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        artifact_phase_executor=RecordingArtifactExecutor(),
    )
    assert isinstance(service._lifecycle, _ObservingLifecycle)
    service._lifecycle._healthy = True
    for _ in range(4):
        restarted.tick()
    recovered = restarted.get(operation.operation_id)
    assert recovered.state == "succeeded", recovered.status_reason
    assert _result(recovered).completed_phases.count("start") == 1


@pytest.mark.parametrize("ending", ["recovered", "cancelled", "superseded", "revoked"])
def test_temporary_phase_failure_preserves_exact_intent_across_restart(
    tmp_path: Path, ending: str
) -> None:
    class InterruptedTransfer(RecordingArtifactExecutor):
        def __init__(self):
            super().__init__()
            self.unavailable = True
            self.identities = []

        def execute(self, plan, phase, **kwargs):
            if phase.kind == "transfer":
                self.identities.append((plan.plan_digest, kwargs["request_key"]))
                if self.unavailable:
                    raise httpx2.ConnectError("NAS temporarily disconnected")
            return super().execute(plan, phase, **kwargs)

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    executor = InterruptedTransfer()
    now = [NOW]

    def restarted():
        result = _service(
            sessions,
            now[0],
            lifecycle,
            executor,
            artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        )
        result._clock = lambda: now[0]
        return result

    service = restarted()
    request = _request(sessions, nodes[0])
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        original = dict(row.payload)
    for _ in range(4):
        assert service.tick()
        waiting = service.get(operation.operation_id)
        assert waiting.state == "running", waiting.status_reason
        assert waiting.result is not None and waiting.next_attempt_at is not None
        due = waiting.next_attempt_at
        assert now[0] < due <= now[0] + timedelta(seconds=60)
        service = restarted()
        assert service.tick() is False
        now[0] = due
    if ending == "cancelled":
        service.cancel(
            operation.operation_id,
            actor="admin",
            request_key=str(uuid.uuid4()),
            reason="Stop recovery",
        )
    elif ending in {"superseded", "revoked"}:
        with sessions.begin() as session:
            node = session.get(AgentNode, nodes[0])
            assert node is not None
            if ending == "superseded":
                node.workload_intent_ordinal += 1
            else:
                node.revoked_at = now[0]
    executor.unavailable = False
    service = restarted()
    service.tick()
    current = service.get(operation.operation_id)
    with sessions() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None and row.payload == original
    if ending == "recovered":
        assert current.state == "running"
        assert current.result is not None
        assert "transfer" in current.result.completed_phases
        assert len(executor.identities) == 5
    else:
        assert current.state == ("failed" if ending == "revoked" else "cancelled")
        assert len(executor.identities) == 4
    assert len(set(executor.identities)) == 1


@pytest.mark.parametrize("kind", ["artifact-job-cancellation", "recipe.stop"])
def test_late_dependency_receipt_resumes_original_checkpoint_after_deadline(
    tmp_path: Path, kind: str
) -> None:
    class PendingDependency(RecordingArtifactExecutor):
        pending = True
        calls_while_pending = 0

        def execute(self, plan, phase, **kwargs):
            if self.pending:
                self.calls_while_pending += 1
                raise RunSwitchIssuedWorkloadPending(
                    kind=kind,
                    owner_id="exact-old-owner",
                    job_id=dependency,
                    observe_due_at=NOW,
                    observation_deadline=NOW + timedelta(seconds=120),
                )
            return super().execute(plan, phase, **kwargs)

    dependency = str(uuid.uuid4())
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    executor = PendingDependency()
    now = [NOW + timedelta(seconds=121)]
    service = _service(
        sessions,
        now[0],
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    service._clock = lambda: now[0]
    request = _request(sessions, nodes[0])
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    assert service.tick()
    held = service.get(operation.operation_id)
    assert held.state == "running", held.status_reason
    assert held.result is not None
    assert held.next_attempt_at is not None
    assert held.next_attempt_at == now[0] + timedelta(seconds=60)
    assert held.status_reason is not None and dependency in held.status_reason
    assert service.tick() is False
    executor.pending = False
    now[0] = held.next_attempt_at
    restarted = _service(
        sessions,
        now[0],
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    assert restarted.tick()
    recovered = restarted.get(operation.operation_id)
    assert recovered.state == "running"
    assert (
        recovered.result is not None and "transfer" in recovered.result.completed_phases
    )
    assert executor.calls_while_pending == 1


def test_shared_admission_contention_preserves_operation_for_retry(
    tmp_path: Path,
) -> None:
    """A busy child enqueue must retain intent instead of escaping the worker."""
    from vonk_control.admission_locking import AdmissionLockBusy

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )

    class BusyExecutor(RecordingArtifactExecutor):
        def execute(self, *args, **kwargs):
            raise AdmissionLockBusy("admission lock node is busy")

    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=BusyExecutor(),
    )
    request = _request(sessions, nodes[0])
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    for _ in range(12):
        service.tick()
        with sessions() as session:
            job = session.get(Job, operation.operation_id)
            assert job is not None
            if job.status_reason and "capacity writer" in job.status_reason:
                assert job.state == "running"
                break
    else:
        pytest.fail("shared admission contention did not schedule a durable retry")


@pytest.mark.usefixtures("damaged_json_rows")
def test_stored_run_switch_with_retired_fields_stays_readable_and_new_work_proceeds(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
    )
    request = _request(sessions, nodes[0])
    old = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    with sessions.begin() as session:
        job = session.get(Job, old.operation_id)
        assert job is not None and isinstance(job.payload, dict)
        payload = dict(job.payload)
        plan = dict(cast("dict[str, object]", payload["plan"]))
        # A field a newer Controller retired must not make history unreadable.
        plan["retired_plan_field"] = {"any": "shape"}
        payload["plan"] = plan
        job.payload = payload

    item = operation_item(
        service.activity_provider()
        .list_operations(
            OperationQuery(after=None, limit=10, state=None, node_id=nodes[0])
        )
        .items[0]
    ).model_dump(mode="json")

    assert item["id"] == old.operation_id
    assert item["kind"] != "run-switch-unreadable"
    assert service.get(old.operation_id).operation_id == old.operation_id


SPARK_A = "spk_" + "a" * 32


SPARK_B = "spk_" + "b" * 32


def _uneven_dual_preparation(tmp_path: Path, *, image_by_node, model_by_node):
    """Prepare a two-Spark group whose presence is uneven and byte counts odd."""

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    group = SparkGroup(
        nodes=[
            SparkGroupNode(
                node_id=node_id,
                rank=rank,
                role="entrypoint" if rank == 0 else "worker",
                endpoint_owner=rank == 0,
            )
            for rank, node_id in enumerate((SPARK_A, SPARK_B))
        ]
    )
    image_bytes = plan.runtime_storage.image_bytes
    assert image_bytes is not None
    runtime_storage = plan.runtime_storage.model_copy(
        update={
            "image_bytes": 1025,
            "missing_image_distribution_bytes": sum(image_by_node.values()),
            "missing_image_distribution_bytes_by_node": image_by_node,
        }
    )
    with sessions() as session:
        revision = session.get(CatalogDocumentRevision, request.recipe_revision_id)
        inspection = CompleteArtifactInspector().inspect(
            session,
            model_content_sha256=request.model_content_sha256,
            recipe_revision_id=request.recipe_revision_id,
            node_ids=(SPARK_A, SPARK_B),
            now=NOW,
        )
    inspection = replace(
        inspection,
        missing_spark_bytes=sum(model_by_node.values()),
        missing_spark_bytes_by_node=model_by_node,
    )
    preparation = RunSwitchOperationService._preparation(
        revision=revision,
        group=group,
        inspection=inspection,
        build=None,
        build_candidate=None,
        runtime_storage=runtime_storage,
        now=NOW,
        reasons=(),
    )
    return plan, group, runtime_storage, inspection, preparation


def test_image_present_on_one_spark_with_odd_bytes_is_prepared_per_node(
    tmp_path: Path,
) -> None:
    """Regression: total / N raised run-switch.per-target-byte-evidence-invalid."""

    *_, preparation = _uneven_dual_preparation(
        tmp_path,
        image_by_node={SPARK_A: 0, SPARK_B: 1025},
        model_by_node={SPARK_A: 0, SPARK_B: 0},
    )
    assert preparation is not None
    states = {t.node_id: t for t in preparation.runtime_image.targets}
    assert states[SPARK_A].state == "ready"
    assert (states[SPARK_A].present_bytes, states[SPARK_A].missing_bytes) == (
        1025,
        0,
    )
    assert states[SPARK_B].state == "unknown"
    assert (states[SPARK_B].present_bytes, states[SPARK_B].missing_bytes) == (
        0,
        1025,
    )
    assert not preparation.ready


def test_model_present_on_one_spark_is_prepared_per_node(tmp_path: Path) -> None:
    *_, preparation = _uneven_dual_preparation(
        tmp_path,
        image_by_node={SPARK_A: 0, SPARK_B: 0},
        model_by_node={SPARK_A: 1024, SPARK_B: 0},
    )
    assert preparation is not None
    states = {t.node_id: t for t in preparation.model.targets}
    assert states[SPARK_A].state == "unknown"
    assert states[SPARK_A].missing_bytes == 1024
    assert states[SPARK_B].state == "ready"
    assert states[SPARK_B].present_bytes == 1024


def test_missing_per_node_evidence_means_missing_here_never_an_error(
    tmp_path: Path,
) -> None:
    plan, group, runtime_storage, inspection, _ = _uneven_dual_preparation(
        tmp_path,
        image_by_node={SPARK_A: 0, SPARK_B: 1025},
        model_by_node={SPARK_A: 0, SPARK_B: 0},
    )
    # A plan or inspection persisted before node-keyed evidence existed.
    legacy_storage = runtime_storage.model_copy(
        update={"missing_image_distribution_bytes_by_node": None}
    )
    preparation = RunSwitchOperationService._preparation(
        revision=None,
        group=group,
        inspection=inspection,
        build=None,
        build_candidate=None,
        runtime_storage=legacy_storage,
        now=NOW,
        reasons=(),
    )
    assert preparation is None  # no revision: unchanged honest-identity rule
    assert _node_missing_bytes(None, SPARK_A, 1025, 2) is None
    assert _node_missing_bytes(None, SPARK_A, 0, 2) == 0
    assert _node_missing_bytes(None, SPARK_A, 1025, 1) == 1025
    assert _node_missing_bytes({SPARK_B: 5}, SPARK_B, 9, 2) == 5

    uneven = plan.model_copy(
        update={
            "spark_group": group,
            "storage": plan.storage.model_copy(
                update={
                    "missing_spark_bytes": 1024,
                    "missing_spark_bytes_by_node": {SPARK_A: 0, SPARK_B: 1024},
                }
            ),
            "runtime_storage": runtime_storage,
        }
    )
    _total, per_node = _planned_transfer_bytes(uneven)
    assert per_node == {SPARK_A: 0, SPARK_B: 1024 + 1025}


def test_stop_verification_keeps_waiting_for_a_lost_run_that_is_not_stopped(
    tmp_path: Path,
) -> None:
    """A lost run still owning residue is not a verified stop and not a failure.

    Wrong implementation this catches: the wait was keyed on the active-run
    set, which excludes ``lost``, so the stop verification failed outright
    instead of waiting for the teardown of the residue to converge.
    """

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    ranks = [
        SimpleNamespace(
            node_id=nodes[0], rank=0, role="entrypoint", state="running", fresh=True
        )
    ]
    status = SimpleNamespace(
        state="lost",
        route_state="published",
        route_error=None,
        healthy=False,
        run_generation=1,
        ranks=ranks,
        recovery_owners=(),
        route_recovery_pending=False,
        route_next_attempt_at=None,
        observation_deadline_at=None,
    )
    stub = SimpleNamespace(run_status=lambda _run_id: status)
    service = RecipeLifecyclePhaseExecutor(
        stub,  # type: ignore[arg-type]
        sessions,
        ClusterMappingService(sessions),
        lifecycle._clock,
    )
    plan = SimpleNamespace(
        action="stop", run_id=str(uuid.uuid4()), profile_stop_scope=None, alias="x"
    )

    execution = service.execute(
        plan,  # type: ignore[arg-type]
        RunSwitchPhase(
            index=0,
            kind="final_verify",
            state="planned",
            detail="Verify lifecycle state",
        ),
        item_index=0,
        actor="admin",
        request_key=str(uuid.uuid4()),
        progress=RunSwitchOperationResult(),
    )

    assert execution.waiting is True
    assert execution.result is not None
    assert isinstance(execution.result, RunSwitchFinalVerifyResult)
    assert execution.result.final_verified is False


def test_installation_verification_refuses_a_lost_run_that_still_has_residue(
    tmp_path: Path,
) -> None:
    """A lost run with residue keeps its installation from being verified.

    Wrong implementation this catches: the active-run count excluded ``lost``,
    so an installation whose lost run still owned ranks verified as free.
    """

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installation.owner_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    with sessions.begin() as session:
        row = session.get(RecipeRun, run.owner_id)
        assert row is not None
        row.state = "lost"
        row.route_state = "withdrawn"
    service = RecipeLifecyclePhaseExecutor(
        lifecycle,
        sessions,
        ClusterMappingService(sessions),
        lifecycle._clock,
    )

    def bound(session, _plan, installation_id, _mapping_id, _digest):
        stored = session.get(RecipeInstallation, installation_id)
        members = tuple(
            session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation_id
                )
            )
        )
        return stored, members

    service._bound_installation = bound  # type: ignore[method-assign,assignment]
    plan = SimpleNamespace(
        action="install",
        installation_id=installation.owner_id,
        mapping=SimpleNamespace(mapping_id=mapping_id),
    )

    with pytest.raises(Exception) as _ending:
        service.execute(
            plan,  # type: ignore[arg-type]
            RunSwitchPhase(
                index=0,
                kind="final_verify",
                state="planned",
                detail="Verify lifecycle state",
            ),
            item_index=0,
            actor="admin",
            request_key=str(uuid.uuid4()),
            progress=RunSwitchOperationResult(),
        )


@pytest.mark.parametrize(
    ("run_state", "released_after_sample", "admitted"),
    [
        ("stopped", True, True),
        # Wrong implementation: a run that failed without a Stop receipt may
        # still hold its memory, so its released claim is never credited.
        ("failed", True, False),
        # Wrong implementation: a release the sample already includes was
        # credited a second time.
        ("stopped", False, False),
    ],
)
def test_memory_freed_by_a_confirmed_stop_after_the_sample_is_not_charged_again(
    tmp_path: Path, run_state: str, released_after_sample: bool, admitted: bool
) -> None:
    """The inventory sample can predate the Stop that freed the memory.

    The load after a failed one was refused ("Observed free capacity less
    definite claims ... leaves N bytes before the required reserve") because the
    sample, up to five minutes old, still counted the memory of a run the
    Controller had since stopped and released.
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
    run_id = lifecycle._run_admission.accept_run(
        run_plan, actor="admin", now=lifecycle._clock()
    )
    request = _request(sessions, node_id, action="switch")
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
        phase_executor=SynchronousPhaseExecutor(),
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = run_state
        run.route_state = "withdrawn"
        for item in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            item.state = "stopped"
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
            )
        ):
            reservation.state = "released"
            reservation.released_at = lifecycle._clock() - timedelta(
                seconds=10 if released_after_sample else 120
            )
    baseline = service.preview(request, actor="admin")
    required = baseline.fit_current.nodes[0].memory_required_bytes
    floor = baseline.fit_current.nodes[0].memory_floor_bytes
    assert required is not None and floor is not None
    # The sample still shows the stopped run's memory in use: free is short of
    # the demand plus the reserve, while the total is ample.
    free = required + floor - 52
    total = free + 1_000_000
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.observed_at = lifecycle._clock() - timedelta(seconds=60)
        snapshot.received_at = lifecycle._clock() - timedelta(seconds=60)
        snapshot.host_memory_total_bytes = total
        snapshot.host_memory_free_bytes = free
        snapshot.gpu_memory_total_bytes = total
        snapshot.gpu_memory_free_bytes = free
    plan = service.preview(request, actor="admin")

    # The released claim is credited exactly when the sample predates the
    # Stop; without the credit the estimate is tried, with a warning, as the
    # only claim on the Spark.
    assert plan.allowed
    available = plan.fit_current.nodes[0].memory_available_bytes
    assert available is not None
    assert (available >= required + floor) is admitted
    assert any("only claim" in warning.detail for warning in plan.warnings) is (
        not admitted
    )

    # A refused preview must not poison admission when fresh inventory fits.
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.observed_at = lifecycle._clock()
        snapshot.received_at = lifecycle._clock()
        snapshot.host_memory_free_bytes = total
        snapshot.gpu_memory_free_bytes = total
    assert service.preview(request, actor="admin").allowed
