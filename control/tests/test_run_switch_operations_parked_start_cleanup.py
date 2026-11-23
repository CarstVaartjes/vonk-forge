"""Run/Switch operations: parked start cleanup."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.state_machines import ReservationState
from vonk_control.install_admission.contracts import (
    AdmissionReason,
)
from vonk_control.models.execution import (
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation, AgentOperationAttempt
from vonk_control.operations.blockers import PHASE_RETRY_CODE
from vonk_control.run_admission import RunAdmissionBusy
from vonk_control.run_switch_contract.phase_results import (
    RunSwitchCleanupVerifyResult,
    RunSwitchUninstallResult,
)
from vonk_control.run_switch_contract.requests import (
    RunSwitchCleanupApplyRequest,
    RunSwitchCleanupPreviewRequest,
)
from vonk_control.run_switch_operations.constants import (
    _FINAL_VERIFICATION_MAX_SECONDS,
)
from vonk_control.run_switch_operations.result_helpers import _read_progress
from vonk_control.run_switch_operations.service import RunSwitchOperationService

from .recipe_operations_support import NOW, installed_recipe, setup_services
from .run_switch_operations_support import (
    RecordingArtifactExecutor,
    _child_operation_id,
    _make_install_specs_missing_placement_authority,
    _ObservingLifecycle,
    _parked_start_switch,
    _record_successful_reconcile_member,
    _result,
    _service,
)


def _admission_delay(reason: str | None, attempt: int) -> int:
    """The delay a capacity wait names: the lifecycle core's backoff (2, 4, 8 ...)."""

    found = re.search(rf"admission retry {attempt} in (\d+)s", reason or "")
    assert found is not None, reason
    return int(found.group(1))


def test_parked_start_with_an_established_run_completes_without_an_operator(
    tmp_path: Path,
) -> None:
    """A lost start acknowledgement must not require manual recovery.

    The Controller never received the start result, but the run is up and
    published.  The lifecycle owner observes that exact run, the phase accepts
    the established effect, and the ordinary final verification confirms it.
    """

    service, operation, start_index = _parked_start_switch(tmp_path, healthy=True)

    for _ in range(4):
        if service.get(operation.operation_id).state not in {"queued", "running"}:
            break
        service.tick()

    view = service.get(operation.operation_id)
    assert view.state == "succeeded", view.status_reason
    completed = _result(view).completed_phases
    # The parked start was accepted exactly once, through the ordinary
    # checkpoint, so no effect is repeated by the observation.
    assert completed.count("start") == 1
    assert "final_verify" in completed
    # The start phase was accepted at its own checkpoint, not replayed earlier.
    assert start_index < len(completed)


def test_parked_start_after_observation_deadline_keeps_exact_effect_pending(
    tmp_path: Path,
) -> None:
    """A deadline slows observation but cannot declare an unknown effect failed."""

    service, operation, _start_index = _parked_start_switch(tmp_path, healthy=False)
    now = [NOW]
    service._clock = lambda: now[0]
    assert service.tick() is True
    now[0] += timedelta(seconds=121)
    assert service.tick() is True

    view = service.get(operation.operation_id)
    assert view.state == LifecycleState.OBSERVING
    assert view.progress.state == LifecycleState.OBSERVING
    assert view.result is not None
    assert view.next_attempt_at is not None
    # One retry clock: the lifecycle core's bounded backoff, never past a minute.
    assert now[0] < view.next_attempt_at <= now[0] + timedelta(seconds=60)
    assert service.tick() is False
    assert isinstance(service._lifecycle, _ObservingLifecycle)
    service._lifecycle._healthy = True
    now[0] = view.next_attempt_at
    for _ in range(4):
        service.tick()
    assert service.get(operation.operation_id).state == "succeeded"


def test_legacy_operator_wait_without_a_clock_is_healed_not_left_waiting(
    tmp_path: Path,
) -> None:
    """No Run/Switch action exists, so a wait for an operator can never end.

    A ``needs-operator`` row with no observation clock is
    re-evaluated by its first advance (core rules 1 and 3): it is retried at the
    core's bounded backoff, then observes its child again and completes.
    """

    service, operation, _start_index = _parked_start_switch(tmp_path, healthy=False)
    now = [NOW]
    service._clock = lambda: now[0]
    with service._sessions.begin() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        assert isinstance(row.result, dict)
        row.state = "needs-operator"
        result = dict(row.result)
        result.pop("observation_due_at", None)
        row.result = result

    held = service.get(operation.operation_id)
    assert held.state == LifecycleState.NEEDS_OPERATOR
    assert service.tick() is True  # healed: it is observed again, not parked
    healed = service.get(operation.operation_id)
    assert healed.state != LifecycleState.NEEDS_OPERATOR
    assert healed.result is not None
    assert healed.next_attempt_at is not None
    assert healed.next_attempt_at > now[0]


def test_parked_start_still_progressing_is_observed_before_final_success(
    tmp_path: Path,
) -> None:
    """A lost start result during a real load is not a failed launch."""

    service, operation, _ = _parked_start_switch(tmp_path, healthy=False)
    now = [NOW]
    service._clock = lambda: now[0]

    assert service.tick() is True
    held = service.get(operation.operation_id)
    assert held.state == "running"
    assert held.result is not None
    assert held.result.child_operation_id is not None
    assert held.next_attempt_at is not None

    assert service.tick() is False
    assert isinstance(service._lifecycle, _ObservingLifecycle)
    service._lifecycle._healthy = True
    now[0] += timedelta(seconds=5)
    for _ in range(4):
        service.tick()
    assert service.get(operation.operation_id).state == "succeeded"


def test_restart_interrupted_start_keeps_exact_child_when_effect_is_uncertain(
    tmp_path: Path,
) -> None:
    service, operation, _ = _parked_start_switch(tmp_path, healthy=False)
    with service._sessions.begin() as session:
        parent = session.get(Job, operation.operation_id)
        assert parent is not None
        assert isinstance(parent.result, dict)
        child_id = parent.result.get("child_operation_id")
        assert isinstance(child_id, str)
        child = session.get(Job, child_id)
        assert child is not None
        owner_id = child.payload.get("owner_id")
        assert isinstance(owner_id, str)
        run = session.get(RecipeRun, owner_id)
        assert run is not None
        run.state = "lost"
        for node in session.scalars(select(RunNode).where(RunNode.run_id == owner_id)):
            node.state = "failed"

    assert service.tick() is True
    view = service.get(operation.operation_id)
    assert view.state == LifecycleState.OBSERVING
    assert view.progress.state == LifecycleState.OBSERVING
    assert view.result is not None
    assert view.result.child_operation_id == child_id


def test_restart_retry_due_is_projected_without_replacing_the_child(
    tmp_path: Path,
) -> None:
    service, operation, _ = _parked_start_switch(tmp_path, healthy=False)
    due = NOW + timedelta(seconds=7)
    with service._sessions.begin() as session:
        parent = session.get(Job, operation.operation_id)
        assert parent is not None
        assert isinstance(parent.result, dict)
        child_id = parent.result.get("child_operation_id")
        assert isinstance(child_id, str)
        child = session.get(Job, child_id)
        assert child is not None
        child.state = "queued"
        agent_child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == child_id)
        )
        assert agent_child is not None
        agent_child.state = "backoff"
        agent_child.next_action_at = due
        agent_child.status_reason = "exact lifecycle retry scheduled"

    assert service.tick() is True
    view = service.get(operation.operation_id)
    assert view.state == "running"
    assert view.result is not None
    assert view.result.child_operation_id == child_id
    assert view.next_attempt_at == due


def test_scoped_cleanup_is_allowed_without_launch_readiness(tmp_path: Path) -> None:
    """Removing work must not depend on being able to start work.

    The launch planner is made unavailable, so a removal plan that consulted
    launch readiness would fail here exactly as it would on a Spark whose
    admission evidence has aged out.
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

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("launch admission is unavailable")

    lifecycle.preview_run = unavailable  # type: ignore[method-assign]
    try:
        cleanup = service.preview_cleanup(installation.owner_id, actor="admin")
    finally:
        del lifecycle.preview_run

    assert cleanup.allowed is True, [reason.code for reason in cleanup.blockers]
    assert cleanup.action == "cleanup"
    assert [phase.kind for phase in cleanup.phases] == ["uninstall", "final_verify"]


def test_scoped_cleanup_is_blocked_while_a_run_is_active(tmp_path: Path) -> None:
    """The uninstall assessment stays the authority for whether removal may run."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    run_plan = lifecycle.preview_run(installation.owner_id, "qwen")
    lifecycle.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )

    cleanup = service.preview_cleanup(installation.owner_id, actor="admin")

    assert cleanup.allowed is False


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
        assert child_job is not None
        assert child_job.kind == "recipe.reconcile"
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
        assert held
        assert all(item.state == "active" for item in held)

    lifecycle.record_node_result(child_id, nodes[1], succeeded=True, evidence={})
    with sessions() as session:
        row = session.get(RecipeInstallation, installation.owner_id)
        assert row is not None
        assert row.state == "uninstalled"
        released = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == installation.owner_id
                )
            )
        )
        assert released
        assert all(item.state == "released" for item in released)

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
        assert partial is not None
        assert partial.state == "partial"
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
        assert completed is not None
        assert completed.state == "uninstalled"
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == installation.owner_id
                )
            )
        )
        assert claims
        assert all(item.state == ReservationState.RELEASED for item in claims)


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
        assert installation is not None
        assert installation.state == "planned"
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
        assert installation is not None
        assert installation.state == "uninstalled"
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
        assert installation is not None
        assert installation.state == "planned"
