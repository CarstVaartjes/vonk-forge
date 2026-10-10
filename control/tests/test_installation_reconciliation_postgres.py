"""Exact reconciliation races and partial receipts through PostgreSQL."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentResult, ReservationState
from vonk_control.agent_jobs import AgentJobService
from vonk_control.models import (
    AgentOperation,
    AgentOperationAttempt,
    AgentPresence,
    InstallationNode,
    Job,
    RecipeInstallation,
    ResourceReservation,
)
from vonk_control.recipe_operations import (
    reconciliation_authority as recipe_operations_module,
)
from vonk_control.run_switch_contract import (
    RunSwitchCleanupApplyRequest,
    RunSwitchCleanupPreviewRequest,
)

from .test_recipe_operations import NOW, installed_recipe, setup_services
from .test_run_switch_operations import (
    RecordingArtifactExecutor,
    _child_operation_id,
    _make_install_specs_missing_placement_authority,
    _record_successful_reconcile_member,
    _service,
)


def _record_original_install_attempts(sessions, installation_id: str) -> None:
    with sessions.begin() as session:
        install_job = session.scalar(
            select(Job).where(
                Job.kind == "recipe.install",
                Job.payload["owner_id"].as_string() == installation_id,
            )
        )
        assert install_job is not None and isinstance(install_job.result, dict)
        evidence_by_node = install_job.result["node_evidence"]
        operations = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == install_job.id)
                .order_by(AgentOperation.node_id)
            )
        )
        for operation in operations:
            evidence = evidence_by_node[operation.node_id]
            presence = session.get(AgentPresence, operation.node_id)
            assert presence is not None
            operation.current_attempt = 1
            session.add(
                AgentOperationAttempt(
                    operation_id=operation.id,
                    attempt=1,
                    fence=str(uuid.uuid4()),
                    lease_deadline=NOW,
                    agent_certificate_serial=presence.certificate_serial,
                    state="succeeded",
                    result=evidence,
                )
            )


def test_postgres_reconcile_lock_excludes_concurrent_group_start(
    tmp_path: Path, postgres_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, node_ids = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        node_ids,
        request_id=str(uuid.uuid4()),
    )
    _record_original_install_attempts(sessions, installation.owner_id)
    authority = lifecycle.preview_reconciliation_authority(installation.owner_id)
    start_plan = lifecycle.preview_run(installation.owner_id, "reconcile-race")
    assert start_plan.allowed, [
        (item.node_id, item.blockers) for item in start_plan.nodes
    ]

    original_lock_rows = recipe_operations_module.lock_admission_rows
    cleanup_has_rows = threading.Event()
    release_cleanup = threading.Event()

    def hold_cleanup_rows(session, requests):
        locked = original_lock_rows(session, requests)
        names = {request.name for request in requests}
        if "reconcile-installation" in names:
            cleanup_has_rows.set()
            assert release_cleanup.wait(timeout=10), (
                "the test did not release the reconciliation transaction"
            )
        return locked

    monkeypatch.setattr(
        recipe_operations_module, "lock_admission_rows", hold_cleanup_rows
    )
    cleanup_request_id = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as workers:
        cleanup = workers.submit(
            lifecycle.reconcile_installation,
            installation.owner_id,
            expected_authority=authority,
            run_switch_plan_digest=authority.original_plan_digest,
            actor="admin",
            request_id=cleanup_request_id,
        )
        try:
            assert cleanup_has_rows.wait(timeout=10)
            with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
                lifecycle.start(
                    start_plan,
                    plan_digest=start_plan.plan_digest,
                    actor="admin",
                    request_id=str(uuid.uuid4()),
                )
        finally:
            release_cleanup.set()
        accepted_cleanup = cleanup.result(timeout=10)

    assert accepted_cleanup.kind == "recipe.reconcile"
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        lifecycle.start(
            start_plan,
            plan_digest=start_plan.plan_digest,
            actor="admin",
            request_id=str(uuid.uuid4()),
        )
    with sessions() as session:
        assert (
            session.scalar(
                select(Job.id).where(
                    Job.kind == "recipe.reconcile",
                    Job.request_id == cleanup_request_id,
                )
            )
            is not None
        )
        assert session.scalar(select(Job.id).where(Job.kind == "recipe.start")) is None


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize(
    "reservation_state", [ReservationState.ACTIVE, ReservationState.RELEASED]
)
def test_postgres_new_review_keeps_the_reconciled_rank_after_a_cancelled_rank(
    tmp_path: Path, postgres_engine, reservation_state: ReservationState
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, node_ids = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installation = installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        node_ids,
        request_id=str(uuid.uuid4()),
    )
    _make_install_specs_missing_placement_authority(
        sessions, installation.owner_id, node_ids
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
    first_child = _child_operation_id(service.get(first.operation_id))
    assert first_child is not None

    _record_successful_reconcile_member(sessions, lifecycle, first_child, node_ids[0])
    with sessions.begin() as session:
        child = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == first_child,
                AgentOperation.node_id == node_ids[1],
            )
        )
        presence = session.get(AgentPresence, node_ids[1])
        assert child is not None and presence is not None
        child.state = "running"
        child.current_attempt = 1
        fence = str(uuid.uuid4())
        deadline = NOW + timedelta(minutes=1)
        session.add(
            AgentOperationAttempt(
                operation_id=child.id,
                attempt=1,
                fence=fence,
                lease_deadline=deadline,
                agent_certificate_serial=presence.certificate_serial,
                state="running",
            )
        )
    lifecycle.cancel(
        first_child,
        actor="admin",
        request_id=str(uuid.uuid4()),
        reason="cancelled reconciliation rank",
    )
    cancelled = AgentResult.model_validate(
        {
            "fence": fence,
            "state": "cancelled",
            "result": {
                "error_code": "operation_cancelled",
                "reason": "the exact cleanup stopped before producing a receipt",
            },
        }
    )
    AgentJobService(
        sessions,
        clock=lambda: NOW,
        result_consumer=lifecycle.consume_agent_result,
    ).record_result(cancelled)
    from vonk_control.run_switch_operations.constants import (
        _FINAL_VERIFICATION_MAX_SECONDS,
    )

    service._clock = lambda: (
        NOW + timedelta(seconds=_FINAL_VERIFICATION_MAX_SECONDS + 1)
    )
    for _ in range(6):
        if service.get(first.operation_id).state not in {"queued", "running"}:
            break
        service.tick()
    assert service.get(first.operation_id).state == "failed"
    with sessions() as session:
        cancelled_job = session.get(Job, first_child)
        assert cancelled_job is not None and cancelled_job.state == "cancelled"

    with sessions() as session:
        partial = session.get(RecipeInstallation, installation.owner_id)
        assert partial is not None and partial.state == "partial"
        member_states = tuple(
            session.execute(
                select(InstallationNode.node_id, InstallationNode.state)
                .where(InstallationNode.installation_id == installation.owner_id)
                .order_by(InstallationNode.rank)
            )
        )
        assert member_states == (
            (node_ids[0], "uninstalled"),
            (node_ids[1], "failed"),
        )
        # Reservation bookkeeping is not cleanup evidence. Admission below
        # must use the retained receipt and accept a fresh cleanup regardless
        # of whether the ended operation's claims have already been reaped.

    with sessions.begin() as session:
        for claim in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == installation.owner_id
            )
        ):
            claim.state = reservation_state
            claim.released_at = (
                service._clock()
                if reservation_state == ReservationState.RELEASED
                else None
            )

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
    ] == [(node_ids[0], "reconciled"), (node_ids[1], "pending")]
    retry = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation.owner_id,
            cleanup_mode="reconcile",
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert service.tick() is True
    retry_child = _child_operation_id(service.get(retry.operation_id))
    assert retry_child is not None
    assert retry.operation_id != first.operation_id
    assert retry_child != first_child
    with sessions() as session:
        children = tuple(
            session.scalars(
                select(Job).where(Job.id == retry_child, Job.kind == "recipe.reconcile")
            )
        )
        assert len(children) == 1
        pending_nodes = children[0].targets
        assert pending_nodes == [node_ids[1]]

    _record_successful_reconcile_member(sessions, lifecycle, retry_child, node_ids[1])
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
