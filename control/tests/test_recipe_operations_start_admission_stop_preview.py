"""Recipe operations: start admission stop preview."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select
from vonk_agent_protocol import (
    ContainerRuntimeAction,
    canonical_message,
)
from vonk_control.bounded_json import require_mapping
from vonk_control.distributed_recovery.coordinator import DistributedRecoveryCoordinator
from vonk_control.host_runtime_plan_authority import derive_runtime_plan_binding
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.jobs.documents import RecipeStopParent
from vonk_control.models.execution import (
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation
from vonk_control.models.routes import RoutePublicationOwner
from vonk_control.recipe_operations.errors import (
    RecipeRequestInvalid,
)

from .recipe_operations_support import (
    NOW,
    ConcurrentPublisher,
    FailingQueue,
    _job_phases,
    _placement,
    _required,
    _typed_stop_payloads,
    bind_route_publications,
    installed_recipe,
    setup_services,
    start_evidence,
    started_recipe,
)


@pytest.mark.parametrize("startup_budget", [60, 1800])
def test_distributed_start_deadline_is_enforced_before_phase_advance(
    tmp_path: Path,
    startup_budget: int,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        nodes=2,
        distributed_lifecycle=True,
        distributed_start_timeout_seconds=startup_budget,
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="v" * 36
    )
    plan = service.preview_run(installation.owner_id, "expired-start")
    start = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="w" * 36,
    )
    with sessions() as session:
        launches = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
        assert len(launches) == 2
        assert all(
            launch.payload["start_deadline"]
            == (NOW + timedelta(seconds=startup_budget)).isoformat()
            for launch in launches
        )
        launch = launches[0]

    service._clock = lambda: NOW + timedelta(seconds=startup_budget)
    service.record_node_result(
        start.id,
        launch.node_id,
        succeeded=True,
        evidence=start_evidence(launch.payload),
    )

    assert service.get(start.id).state == "failed"
    with sessions() as session:
        children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
        assert len(children) == 2
        assert {child.state for child in children} == {"failed", "succeeded"}
        run = _required(session.get(RecipeRun, start.owner_id))
        assert run.state == "stopping"
        assert run.route_state == "withdrawn"
        cleanup = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == start.owner_id,
            )
        )
        assert cleanup is not None
        stop_payloads = _typed_stop_payloads(cleanup.payload)
        assert len(stop_payloads) == len(nodes)
        assert {stop.run_id for stop in stop_payloads} == {start.owner_id}
        assert {stop.target_runtime_id for stop in stop_payloads} == {start.owner_id}
        assert {stop.plan_digest for stop in stop_payloads} == {start.plan_digest}
        assert {stop.run_generation for stop in stop_payloads} == {1}
        assert all(stop.cancel_pending_start for stop in stop_payloads)


def test_failed_start_result_commits_when_cleanup_admission_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused admission never rolls the failed Start's result back: the run is
    left ``stopping`` and the orphaned-run reaper queues its Stop after the grace."""
    from vonk_control.admission_locking import AdmissionLockBusy as NodeLocksHeld
    from vonk_control.owner_liveness import OWNER_SETTLE_GRACE
    from vonk_control.recipe_operations import start_cleanup

    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        nodes=2,
        distributed_lifecycle=True,
        distributed_start_timeout_seconds=60,
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="x" * 36
    )
    plan = service.preview_run(installation.owner_id, "refused-cleanup")
    start = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="y" * 36
    )
    with sessions.begin() as session:
        launches = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
        launch = launches[0]
        for order in launches:
            order.current_attempt = 1  # claimed: the Start did launch
        launch_payload = launch.payload
        launch_node = launch.node_id
    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())

    def refused(*_args: object, **_kwargs: object) -> bool:
        raise NodeLocksHeld("node locks are held")

    monkeypatch.setattr(start_cleanup, "end_unfinished_run", refused)
    service._clock = lambda: NOW + timedelta(seconds=60)
    service.record_node_result(
        start.id,
        launch_node,
        succeeded=True,
        evidence=start_evidence(launch_payload),
    )

    def stops() -> list[Job]:
        with sessions() as session:
            return [
                job
                for job in session.scalars(select(Job).where(Job.kind == "recipe.stop"))
                if job.payload.get("owner_id") == start.owner_id
            ]

    with sessions() as session:
        assert _required(session.get(RecipeRun, start.owner_id)).state == "stopping"
    assert stops() == []

    monkeypatch.undo()
    DistributedRecoveryCoordinator(
        sessions,
        routes=routes,
        agent_jobs=queue,
        clock=lambda: (
            NOW + timedelta(seconds=60) + OWNER_SETTLE_GRACE + timedelta(minutes=1)
        ),
        run_endings=service,
    ).tick()
    with sessions() as session:
        assert _required(session.get(RecipeRun, start.owner_id)).state == "stopping"
    assert len(stops()) == 1


def test_nonzero_endpoint_owner_controls_rendezvous_for_every_rank(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        nodes=2,
        endpoint_owner_rank_one=True,
        distributed_lifecycle=True,
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="5" * 36,
    )
    for node_id in nodes:
        service.record_node_result(
            install.id, node_id, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "nonzero")
    assert next(node for node in run_plan.nodes if node.endpoint_owner).rank == 1
    start = started_recipe(
        sessions,
        service,
        install.owner_id,
        nodes,
        request_id="6" * 36,
        alias="nonzero",
    )
    with sessions() as session:
        job = _required(session.get(Job, start.id))
        payloads = [
            require_mapping(entry["payload"], "job phase entry payload")
            for phase in _job_phases(job.payload)
            for entry in phase
        ]
    placements = [_placement(payload) for payload in payloads]
    assert {item["master_address"] for item in placements} == {"192.168.100.3"}
    assert {item["master_port"] for item in placements} == {29500}
    with sessions() as session:
        run_nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == start.owner_id)
                .order_by(RunNode.rank)
            )
        )
        assert [node.role for node in run_nodes] == ["worker", "entrypoint"]
        assert [node.endpoint is not None for node in run_nodes] == [False, True]
        assert [node.observation_endpoint_ready for node in run_nodes] == [None, True]

    publisher = ConcurrentPublisher()
    _service, routes = bind_route_publications(sessions, service, publisher)
    routes.publish_run(start.owner_id)
    assert publisher.aliases[-1] == ("nonzero",)


def test_install_admission_and_queue_creation_roll_back_together(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)
    service._agent_jobs = FailingQueue()
    plan = service.preview_install(mapping_id, build_id)

    with pytest.raises(Exception) as _ending:
        service.install(
            plan,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="0" * 36,
        )

    with sessions() as session:
        assert list(session.scalars(select(RecipeInstallation))) == []
        assert list(session.scalars(select(ResourceReservation))) == []
        assert list(session.scalars(select(Job).where(Job.kind.like("recipe.%")))) == []


def test_run_admission_and_start_queue_roll_back_together(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="b" * 36,
    )
    service.record_node_result(
        install.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    run_plan = service.preview_run(install.owner_id, "qwen")
    service._agent_jobs = FailingQueue()

    with pytest.raises(Exception) as _ending:
        service.start(
            run_plan,
            plan_digest=run_plan.plan_digest,
            actor="admin",
            request_id="c" * 36,
        )

    with sessions() as session:
        assert list(session.scalars(select(RecipeRun))) == []
        assert (
            list(
                session.scalars(
                    select(ResourceReservation).where(
                        ResourceReservation.owner_kind == "run"
                    )
                )
            )
            == []
        )
        assert session.scalar(select(Job).where(Job.request_id == "c" * 36)) is None


def test_start_orders_carry_the_saved_runs_plan_digest_when_admission_replans(
    tmp_path: Path,
) -> None:
    """The run row is the one reader of the plan digest.

    Admission re-plans under its row locks; observations (inventory time,
    memory figures) are hashed, so a fresh snapshot between the plans gives
    the saved run a different digest than the one the start was planned with.
    The start job and orders must carry the saved run's digest, or the
    host-runtime grant refuses the Start as superseded.
    """

    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="0" * 35 + "7"
    )
    plan = service.preview_run(installation.owner_id, "qwen")
    real_plan_run = service._run_admission.plan_run
    calls: list[str] = []

    def replanning(*args: Any, **kwargs: Any):
        fresh = real_plan_run(*args, **kwargs)
        calls.append(fresh.plan_digest)
        if len(calls) == 1:
            # A newer report lands between the start's plan and admission's.
            InventoryRepository(sessions, clock=lambda: NOW).record(
                NodeInventoryReport.model_validate_json(
                    canonical_message(
                        {
                            "node_id": nodes[0],
                            "observed_at": NOW + timedelta(seconds=1),
                            "disk_total_bytes": 10_000,
                            "disk_free_bytes": 8_000,
                            "host_memory_total_bytes": 10_000,
                            "host_memory_free_bytes": 8_000,
                            "gpu_memory_total_bytes": 10_000,
                            "gpu_memory_free_bytes": 8_000,
                            "gpu_count": 1,
                            "artifact_store_read_only": False,
                            "capabilities": [
                                "runtime.vonk.v1",
                                "recipe.image.pull.v1",
                                "recipe.operations.v1",
                            ],
                            "memory_pool": "shared",
                            "schema_version": 1,
                            "nvidia_driver_version": "unknown",
                            "container_runtime_version": "unknown",
                        }
                    )
                )
            )
        return fresh

    service._run_admission.plan_run = replanning  # type: ignore[method-assign]
    started = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="0" * 35 + "8",
    )

    with sessions() as session:
        run = _required(session.get(RecipeRun, started.owner_id))
        assert calls[0] != run.plan_digest
        job = _required(session.get(Job, started.id))
        assert job.payload["plan_digest"] == run.plan_digest
        operations = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
            )
        )
        assert operations
        for operation in operations:
            assert operation.payload["plan_digest"] == run.plan_digest
            binding = derive_runtime_plan_binding(
                session,
                parent=job,
                operation=operation,
                node_id=operation.node_id,
                action=ContainerRuntimeAction.START,
                stop_plan_sha256=None,
            )
            assert binding.runtime_run_id == run.id


def test_start_uses_current_alias_and_replays_by_request_identity(
    tmp_path: Path,
) -> None:
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="0" * 35 + "1"
    )
    qwen = service.preview_run(installation.owner_id, "qwen")
    alternate = service.preview_run(installation.owner_id, "qwen-alt")

    # The route name is not execution identity: one digest for both names.
    assert qwen.plan_digest == alternate.plan_digest
    started = service.start(
        alternate,
        plan_digest=alternate.plan_digest,
        actor="admin",
        request_id="0" * 35 + "2",
    )
    replayed = service.start(
        alternate,
        plan_digest=alternate.plan_digest,
        actor="admin",
        request_id="0" * 35 + "2",
    )
    assert replayed == started
    assert queue.available == 2
    with sessions() as session:
        run = _required(session.get(RecipeRun, started.owner_id))
        assert run is not None and run.alias == alternate.alias

    with sessions() as session:
        runs = tuple(session.scalars(select(RecipeRun)))
        reservations = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run"
                )
            )
        )
        jobs = tuple(session.scalars(select(Job).where(Job.kind == "recipe.start")))
        operations = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.kind == "recipe.start")
            )
        )
    assert len(runs) == len(jobs) == len(operations) == 1
    assert len(reservations) == 2
    assert queue.available == 2


def test_the_plan_digest_does_not_depend_on_the_route_name(tmp_path: Path) -> None:
    _sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="0" * 35 + "7"
    )
    first = service.preview_run(installation.owner_id, "qwen")
    second = service.preview_run(installation.owner_id, "another-name")
    assert first.alias != second.alias
    assert first.plan_digest == second.plan_digest


def test_start_and_activate_replay_require_the_same_installation(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="0" * 35 + "3"
    )
    plan = service.preview_run(installation.owner_id, "qwen")
    started = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="0" * 35 + "4",
    )
    activation_request_id = "0" * 35 + "5"
    activation_payload = {
        "schema_version": 1,
        "owner_kind": "run",
        "owner_id": started.owner_id,
        "plan_digest": plan.plan_digest,
        "execution_mode": "one-shot-jobs",
    }
    with sessions.begin() as session:
        session.add(
            Job(
                id=str(uuid.uuid4()),
                request_id=activation_request_id,
                kind="recipe.job.activate.v1",
                state="succeeded",
                actor="admin",
                authority_revision="",
                targets=list(nodes),
                payload_digest=hashlib.sha256(
                    canonical_message(activation_payload)
                ).hexdigest(),
                payload=activation_payload,
                result={"activated": True},
                created_at=NOW,
                updated_at=NOW,
            )
        )

    other_installation_id = str(uuid.uuid4())
    other_plan = replace(plan, installation_id=other_installation_id)
    with pytest.raises(Exception) as _ending:
        service.start(
            other_plan,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="0" * 35 + "4",
        )
    with pytest.raises(Exception) as _ending:
        service.activate_job_run(
            other_plan,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=activation_request_id,
        )


def test_adopt_start_binds_the_durable_child_without_reproducing_its_digest(
    tmp_path: Path,
) -> None:
    """Recovery adopts the queued start child before re-reading admission.

    A second preflight of the same installation legitimately derives a
    different digest, because the first start's own reservation is now part of
    the node documents.  A coordinator resuming its own step therefore cannot
    reproduce the digest it queued under, and re-admission would reject the
    unchanged request key.  Adoption binds the durable identity instead:
    request key, operation kind, owner kind, installation and alias.
    """

    sessions, service, queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="0" * 35 + "1"
    )
    qwen = service.preview_run(installation.owner_id, "qwen")
    started = service.start(
        qwen,
        plan_digest=qwen.plan_digest,
        actor="admin",
        request_id="0" * 35 + "3",
    )
    changed = service.preview_run(installation.owner_id, "qwen")
    assert changed.plan_digest != qwen.plan_digest

    adopted = service.adopt_start(installation.owner_id, request_id="0" * 35 + "3")

    assert adopted == started
    # Adoption is scoped to this exact child: another installation or another
    # request key is not it.
    assert (
        service.adopt_start(
            "00000000-0000-4000-8000-000000000000", request_id="0" * 35 + "3"
        )
        is None
    )
    assert service.adopt_start(installation.owner_id, request_id="0" * 35 + "9") is None
    # Replay never queues a second effect for the same step.
    with sessions() as session:
        assert (
            len(tuple(session.scalars(select(Job).where(Job.kind == "recipe.start"))))
            == 1
        )
    assert queue.available == 2


def test_adopt_owned_operation_requires_the_exact_kind_and_owner(
    tmp_path: Path,
) -> None:
    """Scoped cleanup adoption cannot be redirected to another owner."""

    _sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="0" * 35 + "1"
    )
    qwen = service.preview_run(installation.owner_id, "qwen")
    started = service.start(
        qwen,
        plan_digest=qwen.plan_digest,
        actor="admin",
        request_id="0" * 35 + "3",
    )
    stop_plan = service.preview_stop(started.owner_id)
    stopped = service.stop(
        started.owner_id,
        plan_digest=stop_plan.plan_digest,
        actor="admin",
        request_id="0" * 35 + "4",
    )

    assert (
        service.adopt_owned_operation(
            "0" * 35 + "4",
            kind="recipe.stop",
            owner_kind="run",
            owner_id=started.owner_id,
        )
        == stopped
    )
    # The same request key is not this child for another operation kind or of
    # another owner, so cleanup cannot adopt work that was never its own.
    assert (
        service.adopt_owned_operation(
            "0" * 35 + "4",
            kind="recipe.uninstall",
            owner_kind="installation",
            owner_id=installation.owner_id,
        )
        is None
    )
    assert (
        service.adopt_owned_operation(
            "0" * 35 + "4",
            kind="recipe.stop",
            owner_kind="run",
            owner_id="00000000-0000-4000-8000-000000000000",
        )
        is None
    )
    assert (
        service.adopt_owned_operation(
            "0" * 35 + "3",
            kind="recipe.stop",
            owner_kind="run",
            owner_id=started.owner_id,
        )
        is None
    )


def test_stop_state_and_queue_creation_roll_back_together(tmp_path: Path) -> None:
    withdrawn: list[str] = []
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, route_withdrawer=withdrawn.append
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="1" * 35 + "a",
    )
    service.record_node_result(
        install.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    run_plan = service.preview_run(install.owner_id, "qwen")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="1" * 35 + "b",
    )
    with sessions() as session:
        child = _required(
            session.scalar(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
        evidence = start_evidence(child.payload)
    service.record_node_result(start.id, nodes[0], succeeded=True, evidence=evidence)
    service._agent_jobs = FailingQueue()
    plan = service.preview_stop(start.owner_id)

    with pytest.raises(Exception) as _ending:
        service.stop(
            start.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="1" * 35 + "c",
        )

    with sessions() as session:
        assert _required(session.get(RecipeRun, start.owner_id)).state == "running"
        accepted = session.scalar(select(Job).where(Job.request_id == "1" * 35 + "c"))
        assert accepted is not None and accepted.state == "running"
        assert accepted.payload["plan_digest"] == plan.plan_digest
        stop_parent = RecipeStopParent.model_validate_json(
            canonical_message(accepted.payload), strict=True
        )
        assert stop_parent.service_stop_review is not None
        assert stop_parent.service_stop_review.stage == "withdrawal-claimed"
        assert accepted.payload.get("phases") is None
        assert (
            session.scalar(
                select(AgentOperation.id).where(
                    AgentOperation.parent_job_id == accepted.id
                )
            )
            is None
        )
    # Dispatch rolled back without erasing the reviewed owner. The exact same
    # accepted Stop resumes after its queue dependency recovers.
    assert withdrawn == [start.owner_id]


@pytest.mark.parametrize("fault", [RuntimeError, PermissionError, OSError])
def test_stop_withdrawal_failure_retains_accepted_job_and_run_state(
    tmp_path: Path,
    fault: type[RuntimeError | OSError],
) -> None:
    withdrawn: list[str] = []

    def fail_withdrawal(run_id: str) -> None:
        withdrawn.append(run_id)
        raise fault("route withdrawal failed")

    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, route_withdrawer=fail_withdrawal
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="1" * 35 + "d"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="1" * 35 + "e",
    )
    with sessions.begin() as session:
        stored = session.get(RecipeRun, run.owner_id)
        assert stored is not None
        stored.route_state = "published"
    plan = service.preview_stop(run.owner_id)

    if fault is not OSError:
        # Unclassified failures and permission refusals remain explicit.
        with pytest.raises(Exception) as _ending:
            service.stop(
                run.owner_id,
                plan_digest=plan.plan_digest,
                actor="admin",
                request_id="1" * 35 + "f",
            )
    else:
        pending = service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="1" * 35 + "f",
        )
        assert pending.state == "running"

    assert withdrawn == [run.owner_id]
    with sessions() as session:
        stored = session.get(RecipeRun, run.owner_id)
        assert stored is not None
        assert (stored.state, stored.route_state) == ("running", "published")
        accepted = session.scalar(select(Job).where(Job.request_id == "1" * 35 + "f"))
        assert accepted is not None and accepted.state == "running"
        assert accepted.payload["plan_digest"] == plan.plan_digest
        stop_parent = RecipeStopParent.model_validate_json(
            canonical_message(accepted.payload), strict=True
        )
        assert stop_parent.service_stop_review is not None
        assert stop_parent.service_stop_review.stage == "withdrawal-claimed"
        assert accepted.payload.get("phases") is None
        assert (
            session.scalar(
                select(AgentOperation.id).where(
                    AgentOperation.parent_job_id == accepted.id
                )
            )
            is None
        )

    if fault is OSError:
        original_id = accepted.id
        original_ordinal = accepted.payload["workload_intent_ordinal"]
        service._route_withdrawer = withdrawn.append
        resumed = service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="1" * 35 + "f",
        )
        assert resumed.id == original_id
        with sessions() as session:
            stored = session.get(Job, resumed.id)
            assert stored is not None
            assert stored.payload["workload_intent_ordinal"] == original_ordinal
            assert stored.payload.get("phases")


def test_stop_admission_commit_failure_prevents_publication(tmp_path: Path) -> None:
    withdrawn: list[str] = []
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, route_withdrawer=withdrawn.append
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="2" * 35 + "c"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="2" * 35 + "d",
    )
    with sessions.begin() as session:
        stored = session.get(RecipeRun, run.owner_id)
        assert stored is not None
        stored.route_state = "published"
        session.add(
            RoutePublicationOwner(
                singleton_id=1,
                owner_generation=0,
                updated_at=NOW,
            )
        )
    plan = service.preview_stop(run.owner_id)

    def fail_commit(_session) -> None:
        raise RuntimeError("database commit failed")

    event.listen(sessions.class_, "before_commit", fail_commit)
    try:
        with pytest.raises(Exception) as _ending:
            service.stop(
                run.owner_id,
                plan_digest=plan.plan_digest,
                actor="admin",
                request_id="2" * 35 + "e",
            )
    finally:
        event.remove(sessions.class_, "before_commit", fail_commit)

    assert withdrawn == []
    with sessions() as session:
        stored = session.get(RecipeRun, run.owner_id)
        assert stored is not None
        assert (stored.state, stored.route_state) == ("running", "published")
        assert (
            session.scalar(select(Job).where(Job.request_id == "2" * 35 + "e")) is None
        )


def test_stop_preview_is_not_blocked_by_its_reservations(tmp_path: Path) -> None:
    """Reservations never block the Stop that releases them (own_cleanup)."""
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="2" * 35 + "f"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="2" * 35 + "0",
    )
    with sessions.begin() as session:
        reservation = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run.owner_id,
                ResourceReservation.kind == "unified-memory",
            )
        )
        assert reservation is not None
        reservation.plan_digest = "0" * 64

    plan = service.preview_stop(run.owner_id)
    assert plan.allowed is True
    stopped = service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    for node in nodes:
        service.record_node_result(stopped.id, node, succeeded=True, evidence={})
    assert service.get(stopped.id).state == "succeeded"
    # The Spark is free again: a fresh Start of the installation is admitted.
    fresh_plan = service.preview_run(installation.owner_id, "qwen")
    fresh = service.start(
        fresh_plan,
        plan_digest=fresh_plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    assert fresh.state == "running"


@pytest.mark.parametrize("changed_identity", ("run_id", "plan_digest"))
@pytest.mark.usefixtures("damaged_json_rows")
def test_stop_uses_run_ownership_when_start_history_has_changed(
    tmp_path: Path, changed_identity: str
) -> None:
    withdrawn: list[str] = []
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, route_withdrawer=withdrawn.append
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="2" * 35 + "8"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="2" * 35 + "9",
    )
    with sessions.begin() as session:
        parent = _required(session.get(Job, run.id))
        child = _required(
            session.scalar(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == parent.id)
                .order_by(AgentOperation.id)
            )
        )
        parent_payload = json.loads(canonical_message(parent.payload))
        phases = parent_payload["phases"]
        phase_entry = next(
            entry
            for phase in phases
            for entry in phase
            if entry["operation_id"] == child.id
        )
        changed_start = dict(child.payload)
        changed_start[changed_identity] = (
            "different-run" if changed_identity == "run_id" else "0" * 64
        )
        phase_entry["payload"] = changed_start
        child.payload = changed_start
        child.payload_digest = hashlib.sha256(
            canonical_message(changed_start)
        ).hexdigest()
        parent.payload = parent_payload
        parent.payload_digest = hashlib.sha256(
            canonical_message(parent_payload)
        ).hexdigest()

    plan = service.preview_stop(run.owner_id)
    stopped = service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="2" * 35 + "a",
    )
    assert stopped.owner_id == run.owner_id
    assert withdrawn == [run.owner_id]
    with sessions() as session:
        stored = _required(session.get(RecipeRun, run.owner_id))
        assert stored.state == "stopping"
        stops = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == stopped.id)
            )
        )
        assert stops
        assert all(
            child.payload["run_id"] == run.owner_id
            and child.payload["plan_digest"] == stored.plan_digest
            for child in stops
        )


def test_stop_preview_is_stable_exact_and_defers_capacity_release(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="2" * 35 + "a"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="2" * 35 + "b",
    )

    first = service.preview_stop(run.owner_id)
    second = service.preview_stop(run.owner_id)

    assert second == first
    assert first.allowed is True
    assert first.run_id == run.owner_id
    assert first.installation_id == installation.owner_id
    assert first.authority_digest == run.plan_digest
    assert first.route_withdrawal is True
    assert [(node.node_id, node.rank, node.role) for node in first.nodes] == [
        (nodes[0], 0, "entrypoint"),
        (nodes[1], 1, "worker"),
    ]
    assert [node.active_memory_reservation_bytes for node in first.nodes] == [225, 225]
    assert first.total_active_memory_reservation_bytes == 450
    assert len(first.plan_digest) == 64


def test_stop_refuses_changed_review_before_withdrawal_or_admission(
    tmp_path: Path,
) -> None:
    withdrawn: list[str] = []
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, route_withdrawer=withdrawn.append
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="3" * 35 + "a"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="3" * 35 + "b",
    )
    with sessions() as session:
        ordinals = {
            node.node_id: node.workload_intent_ordinal
            for node in session.scalars(select(AgentNode))
        }
    # A run that can no longer be stopped is the one refusal a Stop has.
    with sessions.begin() as session:
        stopped = session.get(RecipeRun, run.owner_id)
        assert stopped is not None
        stopped.state = "stopped"
    with pytest.raises(RecipeRequestInvalid):
        service.stop(
            run.owner_id, plan_digest="0" * 64, actor="admin", request_id="3" * 35 + "c"
        )
    assert withdrawn == []
    with sessions() as session:
        assert (
            session.scalar(select(Job).where(Job.request_id == "3" * 35 + "c")) is None
        )
        assert {
            node.node_id: node.workload_intent_ordinal
            for node in session.scalars(select(AgentNode))
        } == ordinals
