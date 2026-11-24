"""Recipe operations: stop uninstall replay."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from vonk_forge_contracts import (
    RecipeDefinition,
    document_sha256,
)

from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.recipe_operations import (
    RecipeInstallPayload,
    RecipeStartPayload,
    RecipeStopPayload,
)
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.admission_locking import ConstraintReplay
from vonk_control.agent_jobs.service import AgentJobService
from vonk_control.bounded_json import require_mapping, require_sequence
from vonk_control.models.catalog import (
    CatalogDocumentRevision,
)
from vonk_control.models.execution import (
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation, AgentOperationAttempt
from vonk_control.recipe_operations.execution_contract import parse_stored_run_plan
from vonk_control.recipe_operations.interfaces import UndispatchedStopView
from vonk_control.recipe_operations.lifecycle_contract import (
    RecipeOperationResult,
)

from .insert_collision_support import Rendezvous
from .recipe_operations_support import (
    NOW,
    FailingQueue,
    _placement,
    _required,
    installed_recipe,
    setup_services,
    start_evidence,
    started_recipe,
)


def test_stop_replay_is_bound_to_selected_run_kind_and_action_digest(
    tmp_path: Path,
) -> None:
    withdrawn: list[str] = []
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, route_withdrawer=withdrawn.append
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="4" * 35 + "a"
    )
    first_run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="4" * 35 + "b",
        alias="first",
    )
    second_run_id = str(uuid.uuid4())
    second_authority = "9" * 64
    with sessions.begin() as session:
        source = session.get(RecipeRun, first_run.owner_id)
        source_nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == first_run.owner_id)
                .order_by(RunNode.rank)
            )
        )
        assert source is not None
        second_plan_document = {**source.plan, "plan_digest": second_authority}
        session.add(
            RecipeRun(
                id=second_run_id,
                installation_id=source.installation_id,
                mapping_id=source.mapping_id,
                mapping_generation=source.mapping_generation,
                alias="second",
                plan_digest=second_authority,
                plan=second_plan_document,
                state="running",
                route_state="published",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add_all(
            RunNode(
                run_id=second_run_id,
                node_id=node.node_id,
                rank=node.rank,
                role=node.role,
                state=node.state,
                port=node.port + 10,
                reserved_memory_bytes=node.reserved_memory_bytes,
                updated_at=NOW,
            )
            for node in source_nodes
        )
    first_plan = service.preview_stop(first_run.owner_id)
    second_plan = service.preview_stop(second_run_id)
    request_key = "4" * 35 + "d"

    operation = service.stop(
        first_run.owner_id,
        plan_digest=first_plan.plan_digest,
        actor="admin",
        request_id=request_key,
    )
    replay = service.stop(
        first_run.owner_id,
        plan_digest=first_plan.plan_digest,
        actor="admin",
        request_id=request_key,
    )
    with pytest.raises(Exception) as _ending:
        service.stop(
            second_run_id,
            plan_digest=second_plan.plan_digest,
            actor="admin",
            request_id=request_key,
        )

    assert replay == operation
    assert operation.plan_digest == first_plan.plan_digest
    assert operation.nodes == tuple(sorted(nodes))
    assert withdrawn == [first_run.owner_id]
    assert queue.available == 4
    with sessions() as session:
        children = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == operation.id)
                .order_by(AgentOperation.node_id)
            )
        )
        assert len(children) == 1
        assert children[0].node_id == nodes[0]
        stop_payload = RecipeStopPayload.model_validate_json(
            canonical_message(children[0].payload), strict=True
        )
        assert stop_payload.run_id == first_run.owner_id
        assert stop_payload.target_runtime_id == first_run.owner_id
        assert stop_payload.plan_digest == first_run.plan_digest
        assert stop_payload.run_generation == 1
        assert stop_payload.cancel_pending_start is True
        assert {child.authority_revision for child in children} == {
            first_run.plan_digest
        }
        assert {child.payload["plan_digest"] for child in children} == {
            first_run.plan_digest
        }


@pytest.mark.parametrize("dispatch_before_acceptance", [True, False])
def test_duplicate_stop_replays_when_dispatch_wins_continuation_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dispatch_before_acceptance: bool,
) -> None:
    """A committed duplicate wins at admission or before the withdrawal claim."""
    withdrawn: list[str] = []
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, route_withdrawer=withdrawn.append
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    run = started_recipe(
        sessions, service, installation.owner_id, nodes, request_id=str(uuid.uuid4())
    )
    plan = service.preview_stop(run.owner_id)
    request_key = str(uuid.uuid4())
    accept = service._accept_service_stop
    winner_id: str | None = None

    def duplicate() -> str:
        return service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_key,
        ).id

    def raced_accept(
        run_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None,
        profile_target_node_ids: Sequence[str] | None,
        profile_application_id: str | None,
    ) -> Job:
        nonlocal winner_id
        # Restore first so the winning request follows the real execution path.
        monkeypatch.setattr(service, "_accept_service_stop", accept)
        if dispatch_before_acceptance:
            winner_id = duplicate()
        accepted = accept(
            run_id,
            plan_digest=plan_digest,
            actor=actor,
            request_id=request_id,
            workload_intent_ordinal=workload_intent_ordinal,
            profile_target_node_ids=profile_target_node_ids,
            profile_application_id=profile_application_id,
        )
        if not dispatch_before_acceptance:
            winner_id = duplicate()
        return accepted

    monkeypatch.setattr(service, "_accept_service_stop", raced_accept)
    assert duplicate() == winner_id
    assert withdrawn == [run.owner_id]
    with sessions() as session:
        assert (
            len(
                tuple(session.scalars(select(Job).where(Job.request_id == request_key)))
            )
            == 1
        )
        assert (
            len(
                tuple(
                    session.scalars(
                        select(AgentOperation).where(
                            AgentOperation.parent_job_id == winner_id
                        )
                    )
                )
            )
            == 1
        )

    assert winner_id is not None
    service.record_node_result(winner_id, nodes[0], succeeded=True, evidence={})
    assert service.get(winner_id).state == LifecycleState.SUCCEEDED.value
    fresh_plan = service.preview_run(installation.owner_id, "qwen")
    fresh = service.start(
        fresh_plan,
        plan_digest=fresh_plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    assert fresh.id != winner_id
    assert fresh.state == LifecycleState.RUNNING.value


def test_stop_replay_lock_wait_returns_the_committed_undispatched_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A creation race whose replay hits the lock wait answers with the Stop that
    committed, not a raw database error."""

    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    run = started_recipe(
        sessions, service, installation.owner_id, nodes, request_id=str(uuid.uuid4())
    )
    plan = service.preview_stop(run.owner_id)
    request_key = str(uuid.uuid4())
    accept = service._accept_service_stop
    calls = 0

    def raced_accept(*args: object, **kwargs: object) -> Job:
        nonlocal calls
        calls += 1
        if calls == 1:
            accept(*args, **kwargs)  # type: ignore[arg-type]  # commits the receipt
            raise ConstraintReplay("uq_jobs_request", "23505")
        lock_timeout = SimpleNamespace(sqlstate="55P03")
        raise OperationalError("replay", {}, lock_timeout)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_accept_service_stop", raced_accept)
    view = service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=request_key,
    )
    with sessions() as session:
        (committed,) = session.scalars(select(Job).where(Job.request_id == request_key))
        assert view == UndispatchedStopView.of(
            service._view(committed, session=session)
        )


def test_concurrent_duplicate_stop_maps_to_one_operation_on_sqlite(
    tmp_path: Path,
) -> None:
    withdrawn: list[str] = []
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, route_withdrawer=withdrawn.append
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="4" * 35 + "e"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="4" * 35 + "f",
    )
    plan = service.preview_stop(run.owner_id)
    start = Rendezvous(2)
    request_key = "4" * 35 + "0"

    def stop() -> str:
        start.wait()
        return service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_key,
        ).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        operation_ids = list(pool.map(lambda _index: stop(), range(2)))

    assert len(set(operation_ids)) == 1
    # Each request withdraws before dispatching (idempotent); one Stop exists.
    assert set(withdrawn) == {run.owner_id}
    with sessions() as session:
        assert (
            len(
                tuple(session.scalars(select(Job).where(Job.request_id == request_key)))
            )
            == 1
        )


def test_partial_multinode_stop_retains_every_active_capacity_reservation(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="5" * 35 + "a"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="5" * 35 + "b",
    )
    with sessions() as session:
        before = len(
            tuple(
                session.scalars(
                    select(ResourceReservation).where(
                        ResourceReservation.owner_kind == "run",
                        ResourceReservation.owner_id == run.owner_id,
                        ResourceReservation.state == "active",
                    )
                )
            )
        )
    plan = service.preview_stop(run.owner_id)
    operation = service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="5" * 35 + "c",
    )

    service.record_node_result(operation.id, nodes[0], succeeded=True, evidence={})
    service.record_node_result(
        operation.id,
        nodes[1],
        succeeded=False,
        evidence={"code": "stop.failed"},
    )

    assert service.get(operation.id).state == "failed"
    with sessions() as session:
        # The unconfirmed rank may still run: lost, never failed.
        assert _required(session.get(RecipeRun, run.owner_id)).state == "lost"
        active = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run.owner_id,
                    ResourceReservation.state == "active",
                )
            )
        )
        assert len(active) == before


def test_partial_install_fails_as_a_group_and_can_retry(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    plan = service.preview_install(mapping_id, build_id)
    first = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="2" * 36
    )
    service.record_node_result(
        first.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    service.record_node_result(
        first.id, nodes[1], succeeded=False, evidence={"code": "pull.failed"}
    )

    assert service.get(first.id).state == "failed"
    first_result = service.get(first.id).lifecycle_result
    assert isinstance(first_result, RecipeOperationResult)
    assert first_result.successful_nodes == [nodes[0]]
    retry = service.retry(first.id, actor="admin", request_id="3" * 36)
    assert retry.id != first.id
    assert retry.owner_id == first.owner_id
    with sessions() as session:
        installation = _required(session.get(RecipeInstallation, retry.owner_id))
        persisted_plans = require_mapping(
            installation.plan["compiled_execution_plans"], "compiled execution plans"
        )
        children = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == retry.id)
                .order_by(AgentOperation.node_id)
            )
        )
        assert {child.node_id for child in children} == set(nodes)
        for child in children:
            parsed = RecipeInstallPayload.model_validate(child.payload)
            assert (
                parsed.compiled_execution_plan.to_mapping()
                == persisted_plans[child.node_id]
            )
    with pytest.raises(Exception) as _ending:
        service.retry(first.id, actor="admin", request_id="3" * 35 + "4")
    with sessions.begin() as session:
        row = _required(session.get(Job, first.id))
        row.result = None
    # A terminal receipt that is gone is shown as absent, not as a refusal.
    assert service.get(first.id).result is None


def test_failed_install_retry_state_rolls_back_when_queue_write_fails(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    plan = service.preview_install(mapping_id, build_id)
    first = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="2" * 35 + "a"
    )
    service.record_node_result(
        first.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    service.record_node_result(
        first.id, nodes[1], succeeded=False, evidence={"code": "pull.failed"}
    )
    with sessions() as session:
        before = {
            node.node_id: node.state
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == first.owner_id
                )
            )
        }
    service._agent_jobs = FailingQueue()

    with pytest.raises(Exception) as _ending:
        service.retry(first.id, actor="admin", request_id="2" * 35 + "b")

    with sessions() as session:
        assert (
            _required(session.get(RecipeInstallation, first.owner_id)).state
            == "partial"
        )
        after = {
            node.node_id: node.state
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == first.owner_id
                )
            )
        }
        assert after == before


def test_new_install_intent_retires_only_unissued_older_install(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    plan = service.preview_install(mapping_id, build_id)
    old = service.install(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="old-install-intent",
    )
    with sessions.begin() as session:
        _required(session.get(AgentNode, nodes[0])).workload_intent_ordinal = 2
    assert service.assess_superseded_unissued("recipe.install", old.owner_id)
    assert service.reconcile_superseded_unissued("recipe.install", old.owner_id, 2)
    new = service.start_installation(
        old.owner_id,
        actor="admin",
        request_id="new-install-intent",
        workload_intent_ordinal=2,
    )
    with sessions() as session:
        assert _required(session.get(Job, old.id)).state == "cancelled"
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == old.id)
        )
        assert child is not None
        assert child.state == "cancelled"
        assert (
            _required(session.get(Job, new.id)).payload["workload_intent_ordinal"] == 2
        )


def test_new_stop_intent_replans_after_unissued_old_stop(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="installed-for-stop-intent"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="running-for-stop-intent",
    )
    old_plan = service.preview_stop(run.owner_id)
    old = service.stop(
        run.owner_id,
        plan_digest=old_plan.plan_digest,
        actor="admin",
        request_id="old-stop-intent",
    )
    with sessions.begin() as session:
        _required(session.get(AgentNode, nodes[0])).workload_intent_ordinal = 4
    prospective = service.preview_stop(run.owner_id)
    assert prospective.allowed
    assert prospective.run_state == "stopping"
    assert service.assess_superseded_unissued("recipe.stop", run.owner_id)
    assert service.reconcile_superseded_unissued("recipe.stop", run.owner_id, 4)
    assert service.preview_stop(run.owner_id).plan_digest == prospective.plan_digest
    new = service.stop(
        run.owner_id,
        plan_digest=prospective.plan_digest,
        actor="admin",
        request_id="new-stop-intent",
        workload_intent_ordinal=4,
    )
    with sessions() as session:
        assert _required(session.get(Job, old.id)).state == "cancelled"
        assert (
            _required(session.get(Job, new.id)).payload["workload_intent_ordinal"] == 4
        )
        assert (
            _required(session.get(RecipeRun, run.owner_id)).route_state == "withdrawn"
        )
        assert (
            session.scalar(
                select(ResourceReservation.id).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run.owner_id,
                    ResourceReservation.state == "active",
                )
            )
            is not None
        )
    obsolete = service.cancel(
        old.id,
        actor="admin",
        request_id=str(uuid.uuid4()),
        reason="new request owns the run",
    )
    assert obsolete.state == "cancelled"
    with pytest.raises(Exception) as _ending:
        service.cancel(
            old.id,
            actor="admin",
            request_id="new-stop-intent",
            reason="new request owns the run",
        )
    with pytest.raises(Exception) as _ending:
        service.cancel(
            old.id,
            actor="admin",
            request_id="not-a-uuid",
            reason="new request owns the run",
        )


def test_issued_stop_is_not_retired_as_unissued(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="installed-for-issued-stop"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="running-for-issued-stop",
    )
    plan = service.preview_stop(run.owner_id)
    old = service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="issued-old-stop",
    )
    with sessions.begin() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == old.id)
        )
        assert child is not None
        child.state = "running"
        child.current_attempt = 1
        session.add(
            AgentOperationAttempt(
                operation_id=child.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                lease_deadline=NOW + timedelta(minutes=1),
                agent_certificate_serial="serial-0",
                state="running",
            )
        )
        _required(session.get(AgentNode, nodes[0])).workload_intent_ordinal = 4
        AgentJobService.request_superseded_workload_cancellation_in_session(
            session, nodes, 4, NOW
        )
    assert not service.assess_superseded_unissued("recipe.stop", run.owner_id)
    assert not service.reconcile_superseded_unissued("recipe.stop", run.owner_id, 4)
    # A newer Stop of the same run observes the issued one, which is cleanup:
    # the newer intent never cancels it.
    pending = service.assess_superseded_issued("recipe.stop", run.owner_id, 4)
    assert pending is not None
    assert pending.job_id == old.id
    assert pending.observe_due_at <= pending.observation_deadline
    fresh = service.preview_stop(run.owner_id)
    assert fresh.allowed
    assert fresh.run_state == "stopping"
    replacement = service.stop(
        run.owner_id,
        plan_digest=fresh.plan_digest,
        actor="admin",
        request_id="issued-new-stop",
        workload_intent_ordinal=4,
    )
    with sessions() as session:
        old_job = _required(session.get(Job, old.id))
        assert old_job.state == "running"
        assert not (old_job.result or {}).get("cancel_requested")
        assert (
            _required(session.get(Job, replacement.id)).payload[
                "workload_intent_ordinal"
            ]
            == 4
        )
        assert _required(session.get(RecipeRun, run.owner_id)).state == "stopping"


def test_new_uninstall_intent_replans_after_unissued_old_uninstall(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service,
        mapping_id,
        build_id,
        nodes,
        request_id="installed-for-uninstall-intent",
    )
    old = service.uninstall(
        installation.owner_id,
        actor="admin",
        request_id="old-uninstall-intent",
    )
    with sessions.begin() as session:
        _required(session.get(AgentNode, nodes[0])).workload_intent_ordinal = 3
    prospective = service.preview_uninstall(installation.owner_id)
    assert prospective.allowed
    assert service.assess_superseded_unissued("recipe.uninstall", installation.owner_id)
    assert service.reconcile_superseded_unissued(
        "recipe.uninstall", installation.owner_id, 3
    )
    assert (
        service.preview_uninstall(installation.owner_id).plan_digest
        == prospective.plan_digest
    )
    new = service.uninstall(
        installation.owner_id,
        actor="admin",
        request_id="new-uninstall-intent",
        workload_intent_ordinal=3,
    )
    with sessions() as session:
        assert _required(session.get(Job, old.id)).state == "cancelled"
        assert (
            _required(session.get(Job, new.id)).payload["workload_intent_ordinal"] == 3
        )


def test_start_stop_and_uninstall_preserve_capacity_safely(tmp_path: Path) -> None:
    def declare_memory_floor(document: dict[str, object]) -> None:
        topology = require_mapping(document["topology"], "recipe topology")
        roles = require_sequence(topology["roles"], "recipe roles")
        entrypoint = require_mapping(roles[0], "entrypoint role")
        resources = require_mapping(entrypoint["resources"], "role resources")
        memory = resources["memory"]
        assert isinstance(memory, dict)
        memory["reserve_bytes"] = 107

    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, recipe_transform=declare_memory_floor
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="4" * 36,
    )
    service.record_node_result(
        install.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )

    run_plan = service.preview_run(install.owner_id, "qwen")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="5" * 36,
    )
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
        )
        assert child is not None
        run = _required(session.get(RecipeRun, start.owner_id))
        expected_floor = parse_stored_run_plan(run.plan).nodes[0].memory_floor_bytes
        # The recipe's 107-byte reserve is informational; the platform floor applies.
        assert expected_floor == 50
        assert _placement(child.payload)["memory_floor_bytes"] == expected_floor
        start_payload = RecipeStartPayload.model_validate(child.payload)
        assert (
            start_payload.compiled_execution_plan.runtime.placement.memory_floor_bytes
            == expected_floor
        )
        child_placement = _placement(child.payload)
        assert child_placement["endpoint_address"] == "192.168.1.211"
        assert child_placement["world_size"] == 1
        assert child_placement["master_address"] is None
        RecipeStartPayload.model_validate(child.payload)
        evidence = start_evidence(child.payload)
    blocked_uninstall = service.preview_uninstall(install.owner_id)
    assert blocked_uninstall.allowed is False
    with pytest.raises(Exception) as _ending:
        service.uninstall(
            install.owner_id,
            actor="admin",
            request_id="6" * 36,
        )

    service.record_node_result(
        start.id,
        nodes[0],
        succeeded=True,
        evidence=evidence,
    )
    assert service.get(start.id).state == "succeeded"
    stop_plan = service.preview_stop(start.owner_id)
    stop = service.stop(
        start.owner_id,
        plan_digest=stop_plan.plan_digest,
        actor="admin",
        request_id="7" * 36,
    )
    assert service.get(stop.id).state == "running"
    next_stop_plan = service.preview_stop(start.owner_id)
    replacement_stop = service.stop(
        start.owner_id,
        plan_digest=next_stop_plan.plan_digest,
        actor="admin",
        request_id="7" * 35 + "a",
    )
    assert replacement_stop.id != stop.id
    # A Stop is cleanup: the newer Stop runs alongside it, never cancels it.
    assert service.get(stop.id).state == "running"
    assert service.get(replacement_stop.id).state == "running"
    service.record_node_result(
        replacement_stop.id, nodes[0], succeeded=True, evidence={}
    )
    with sessions() as session:
        run = _required(session.get(RecipeRun, start.owner_id))
        reservations = list(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == run.id,
                    ResourceReservation.state == "active",
                )
            )
        )
        assert run.state == "stopped"
        assert reservations == []

    uninstall = service.uninstall(
        install.owner_id,
        actor="admin",
        request_id="8" * 36,
    )
    service.record_node_result(uninstall.id, nodes[0], succeeded=True, evidence={})
    with sessions() as session:
        installation = session.get(RecipeInstallation, install.owner_id)
        assert installation is not None
        assert installation.state == "uninstalled"
        revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        assert revision is not None
        assert revision.kind == "recipe"
        assert revision.state == "active"


def test_uninstall_preview_has_exact_bytes_content_and_fixed_consequences(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="8" * 35 + "a"
    )

    first = service.preview_uninstall(installation.owner_id)
    second = service.preview_uninstall(installation.owner_id)

    assert second == first
    assert first.allowed is True
    assert first.installation_id == installation.owner_id
    assert first.original_plan_digest == installation.plan_digest
    assert first.bytes_removed == 240
    assert [
        (node.node_id, node.rank, node.role, node.installed_bytes)
        for node in first.nodes
    ] == [
        (nodes[0], 0, "entrypoint", 120),
        (nodes[1], 1, "worker", 120),
    ]
    assert first.active_runs == ()
    assert first.consequences.catalog_retained is True
    assert first.consequences.automatic_stop is False
    assert first.consequences.reinstall_required is True
    assert first.model_impact.effect == "recipe-and-unused-model"
    assert first.model_impact.dependent_recipe_ids == ()
    assert first.model_impact.cleanup_node_ids == nodes
    assert first.model_impact.retained_node_ids == ()
    with sessions() as session:
        stored = session.get(RecipeInstallation, installation.owner_id)
        assert stored is not None
        revision = session.get(CatalogDocumentRevision, stored.recipe_revision_id)
        assert revision is not None
        assert first.installation_authority_digest == revision.content_digest


@pytest.mark.parametrize("corruption", [None, "schema", "path", "permissions"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_uninstall_validates_stored_identity_without_requiring_launch_placement(
    tmp_path: Path,
    corruption: str | None,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    with sessions.begin() as session:
        stored = _required(session.get(RecipeInstallation, installation.owner_id))
        document = json.loads(json.dumps(stored.plan))
        for compiled in document["compiled_execution_plans"].values():
            # Installation does not have the signed addresses assigned at start.
            compiled["runtime"]["placement"]["master_port"] = 29500
        stored.plan = document

    # The exact persisted document remains inadmissible as an agent launch.
    for compiled in document["compiled_execution_plans"].values():
        with pytest.raises(Exception) as _ending:
            RecipeInstallPayload.model_validate_json(
                json.dumps(
                    {
                        "installation_id": installation.owner_id,
                        "plan_digest": installation.plan_digest,
                        "expected_bytes": 120,
                        "compiled_execution_plan": compiled,
                    }
                )
            )

    if corruption is not None:
        malformed = json.loads(json.dumps(document))
        compiled = malformed["compiled_execution_plans"][nodes[0]]
        if corruption == "schema":
            compiled["schema_version"] = 1
        elif corruption == "path":
            compiled["artifacts"][0]["path"] = "../unrelated/model"
        else:
            compiled["security"]["privileged"] = True
        with sessions.begin() as session:
            _required(
                session.get(RecipeInstallation, installation.owner_id)
            ).plan = malformed
        # A stored plan that does not parse is not a reason to refuse the removal:
        # the accepted ranks are rebuilt from the saved mapping, and the damage is
        # retired as unknown.
        damaged_preview = service.preview_uninstall(installation.owner_id)
        assert damaged_preview.allowed
        assert damaged_preview.nodes
        with sessions() as session:
            assert not list(
                session.scalars(select(Job).where(Job.kind == "recipe.uninstall"))
            )
        return

    preview = service.preview_uninstall(installation.owner_id)
    assert preview.allowed
    operation = service.uninstall(
        installation.owner_id,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    with sessions() as session:
        children = list(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == operation.id
                )
            )
        )
        assert {child.node_id for child in children} == set(nodes)
        assert all(
            child.payload["installation_id"] == installation.owner_id
            for child in children
        )
        assert (
            _required(session.get(RecipeInstallation, installation.owner_id)).plan
            == document
        )


def test_uninstall_keeps_model_when_another_installed_recipe_uses_it(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    first = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="8" * 35 + "1"
    )
    second = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="8" * 35 + "2"
    )

    preview = service.preview_uninstall(first.owner_id)

    assert preview.model_impact.effect == "recipe-only"
    assert preview.model_impact.dependent_recipe_ids == (preview.recipe_id,)
    assert preview.model_impact.cleanup_node_ids == ()
    assert preview.model_impact.retained_node_ids == nodes
    operation = service.uninstall(
        first.owner_id,
        actor="admin",
        request_id="8" * 35 + "3",
    )
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == operation.id)
        )
        assert child is not None
        assert child.payload["cleanup_model_content_sha256"] is None

    service.record_node_result(
        operation.id,
        nodes[0],
        succeeded=True,
        evidence={},
    )
    final_dependent = service.preview_uninstall(second.owner_id)
    assert final_dependent.model_impact.effect == "recipe-and-unused-model"
    assert final_dependent.model_impact.dependent_recipe_ids == ()
    assert final_dependent.model_impact.cleanup_node_ids == nodes
    assert final_dependent.model_impact.retained_node_ids == ()


def test_uninstall_keeps_model_files_another_installation_runs_under_a_new_digest(
    tmp_path: Path,
) -> None:
    """A release re-digested the Model over the same files, and a newer recipe's
    installation runs that set under the new digest. Catches dependents read by
    Model digest alone: the old installation's uninstall planned the files'
    cleanup on a Spark whose other installation still runs them."""

    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    old = installed_recipe(
        service, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    newer = installed_recipe(
        service, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    redigested = "b" * 64
    with sessions.begin() as session:
        installation = _required(session.get(RecipeInstallation, newer.owner_id))
        assert installation.model_artifact_set_sha256 is not None
        recipe = _required(
            session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        )
        current = RecipeDefinition.model_validate(recipe.document)
        model = _required(
            session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "model",
                    CatalogDocumentRevision.content_digest
                    == current.models[0].model.content_sha256,
                )
            )
        )
        document = current.model_dump(mode="json")
        document["models"][0]["model"]["content_sha256"] = redigested
        document = RecipeDefinition.model_validate(document).model_dump(mode="json")
        for kind, source, content, digest in (
            ("model", model, model.document, redigested),
            ("recipe", recipe, document, document_sha256(document)),
        ):
            revision = CatalogDocumentRevision(
                id=str(uuid.uuid4()),
                document_id=source.document_id,
                kind=kind,
                publisher=source.publisher,
                slug=source.slug,
                revision_number=source.revision_number + 1,
                schema_version=source.schema_version,
                state="active",
                document=content,
                content_digest=digest,
                projected={},
                created_by="admin",
                created_at=NOW,
            )
            session.add(revision)
        session.flush()
        installation.recipe_revision_id = revision.id
        installation.model_content_sha256 = redigested

    operation = service.uninstall(
        old.owner_id, actor="admin", request_id=str(uuid.uuid4())
    )
    service.record_node_result(operation.id, nodes[0], succeeded=True, evidence={})

    with sessions() as session:
        child = _required(
            session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == operation.id
                )
            )
        )
        assert child.payload["cleanup_model_content_sha256"] is None
    # The newer recipe still loads from the files its Spark kept.
    run = service.preview_run(newer.owner_id, "kept")
    assert run.allowed, run.nodes
    started = service.start(
        run, plan_digest=run.plan_digest, actor="admin", request_id=str(uuid.uuid4())
    )
    assert started.state in {"queued", "running"}
