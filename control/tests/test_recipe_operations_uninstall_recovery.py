"""Recipe operations: uninstall recovery."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from vonk_agent_protocol import (
    LifecycleState,
    RouteState,
    RunState,
)
from vonk_control.fleet_profile_contract.definitions import FleetProfileInput
from vonk_control.fleet_profiles.factory import build_production_fleet_profile_service
from vonk_control.model_primitives import Base
from vonk_control.models.catalog import (
    CatalogDocumentRevision,
)
from vonk_control.models.execution import (
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation
from vonk_control.recipe_operations.observation_helpers import (
    prepare_exact_recipe_run_observation_nodes,
)
from vonk_control.recipe_routes.shared import RecipeRecoveryDeadlineError
from vonk_control.run_switch_operations.service import RunSwitchOperationService

from .recipe_operations_support import (
    NOW,
    FailingQueue,
    RecordingQueue,
    _placement,
    _postgres_backend_pid,
    _queued_distributed_recovery_restart,
    _queued_distributed_recovery_stop,
    _recovery_deadline,
    _required,
    _wait_for_postgres_block,
    installed_recipe,
    mark_current_exact_observations,
    setup_services,
    start_evidence,
    started_recipe,
)


def test_installations_removed_together_do_not_keep_each_others_model_files(
    tmp_path: Path,
) -> None:
    """The storage sweep removes every installation of a model at once. Each one
    asks the Spark to reclaim the shared files (the Spark removes only what no
    other installation still links), so the last to go frees them: catches an
    eviction that removes installations and frees none of the model bytes."""

    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    first = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="9" * 35 + "1"
    )
    second = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="9" * 35 + "2"
    )

    alone = service.preview_uninstall(first.owner_id)
    together = service.preview_uninstall(
        first.owner_id, also_removing=(second.owner_id,)
    )

    assert alone.model_impact.cleanup_node_ids == ()
    assert together.model_impact.cleanup_node_ids == nodes
    operation = service.uninstall(
        first.owner_id,
        actor="admin",
        request_id="9" * 35 + "3",
        also_removing=(second.owner_id,),
    )
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == operation.id)
        )
        assert child is not None
        assert child.payload["cleanup_model_content_sha256"] is not None


def test_uninstall_cleans_model_per_spark_when_dependency_is_node_local(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    target = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="8" * 35 + "4"
    )
    retained = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="8" * 35 + "5"
    )
    with sessions.begin() as session:
        no_longer_installed = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == retained.owner_id,
                InstallationNode.node_id == nodes[1],
            )
        )
        assert no_longer_installed is not None
        no_longer_installed.state = "uninstalled"

    preview = service.preview_uninstall(target.owner_id)

    assert preview.model_impact.effect == "recipe-and-partial-model-cleanup"
    assert preview.model_impact.dependent_recipe_ids == (preview.recipe_id,)
    assert preview.model_impact.retained_node_ids == (nodes[0],)
    assert preview.model_impact.cleanup_node_ids == (nodes[1],)
    operation = service.uninstall(
        target.owner_id,
        actor="admin",
        request_id="8" * 35 + "6",
    )
    with sessions() as session:
        children = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == operation.id)
                .order_by(AgentOperation.node_id)
            )
        )
    assert [child.payload["cleanup_model_content_sha256"] for child in children] == [
        None,
        preview.model_impact.model_content_sha256,
    ]


def test_uninstall_warns_on_unknown_bytes_but_blocks_active_runs_without_implicit_stop(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="8" * 35 + "b"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="8" * 35 + "c",
    )
    active = service.preview_uninstall(installation.owner_id)
    assert active.allowed is False
    assert [item.run_id for item in active.active_runs] == [run.owner_id]
    with pytest.raises(Exception) as _ending:
        service.uninstall(
            installation.owner_id,
            actor="admin",
            request_id="8" * 35 + "d",
        )
    with sessions() as session:
        assert (
            session.scalar(select(Job).where(Job.request_id == "8" * 35 + "d")) is None
        )
        assert (
            session.scalar(
                select(Job).where(
                    Job.kind == "recipe.stop",
                    Job.payload["owner_id"].as_string() == run.owner_id,
                )
            )
            is None
        )

    with sessions.begin() as session:
        stored_run = session.get(RecipeRun, run.owner_id)
        stored_installation = session.get(RecipeInstallation, installation.owner_id)
        failed_node = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation.owner_id,
                InstallationNode.node_id == nodes[1],
            )
        )
        assert stored_run is not None and stored_installation is not None
        assert failed_node is not None
        stored_run.state = "stopped"
        stored_installation.state = "partial"
        failed_node.state = "failed"
    unknown = service.preview_uninstall(installation.owner_id)
    assert unknown.allowed is True
    assert unknown.bytes_removed is None
    assert unknown.blockers == ()
    # Recovery is asserted through effects and ownership below.
    assert unknown.nodes[1].installed_bytes is None


@pytest.mark.parametrize("first_node_removed", [False, True])
def test_profile_cleanup_new_load_reuses_completed_nodes_after_failed_uninstall(
    tmp_path: Path,
    first_node_removed: bool,
) -> None:
    from .run_switch_operations_support import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
        _child_operation_id,
    )

    sessions, operations, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        operations, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )

    def run_switch():
        return RunSwitchOperationService(
            sessions,
            lifecycle=operations,
            clock=operations._clock,
            artifacts=CompleteArtifactInspector(),
            artifact_phase_executor=RecordingArtifactExecutor(),
            memory_floor_bytes=50,
        )

    switch = run_switch()
    profiles = build_production_fleet_profile_service(
        sessions, clock=operations._clock, run_switch_operations=switch
    )
    profile = profiles.create(
        FleetProfileInput(name="Idle", installation_policy="exact"), actor="admin"
    )
    first = profiles.load(
        profile.number,
        request_key=str(uuid.uuid4()),
        actor="admin",
    )
    assert profiles.tick()
    first_application = profiles.application(first.id)
    assert first_application.current_operation_id == first.id
    first_adapter = first_application.progress.switch_adapter
    assert first_adapter is not None
    first_switch = first_adapter.pending_children[0].operation_id
    assert first_switch is not None
    assert switch.tick()
    first_job = _child_operation_id(switch.get(first_switch))
    assert first_job is not None
    operations.record_node_result(
        first_job,
        nodes[0],
        succeeded=first_node_removed,
        evidence={} if first_node_removed else {"code": "cleanup.failed"},
    )
    operations.record_node_result(
        first_job, nodes[1], succeeded=False, evidence={"code": "cleanup.failed"}
    )
    from vonk_control.run_switch_operations.constants import (
        _FINAL_VERIFICATION_MAX_SECONDS,
    )

    switch._clock = lambda: NOW + timedelta(seconds=_FINAL_VERIFICATION_MAX_SECONDS + 1)
    for _ in range(4):
        if switch.get(first_switch).state == "failed":
            break
        switch.tick()
    assert switch.get(first_switch).state == "failed"
    assert profiles.tick()
    # The Controller retries the failed cleanup by itself: it backs off.
    assert profiles.application(first.id).state == "backoff"

    # A new load has fresh authority but reuses completed node cleanup after restart.
    switch = run_switch()
    profiles = build_production_fleet_profile_service(
        sessions, clock=lambda: NOW + timedelta(seconds=1), run_switch_operations=switch
    )
    request_key = str(uuid.uuid4())
    retry = profiles.load(
        profile.number,
        request_key=request_key,
        actor="admin",
    )
    assert retry.retry_of_application_id is None
    assert retry.progress.workload_intent_ordinal is not None
    assert first.progress.workload_intent_ordinal is not None
    assert (
        retry.progress.workload_intent_ordinal > first.progress.workload_intent_ordinal
    )
    assert profiles.tick()
    second_application = profiles.application(retry.id)
    assert second_application.current_operation_id == retry.id
    second_adapter = second_application.progress.switch_adapter
    assert second_adapter is not None
    second_switch = second_adapter.pending_children[0].operation_id
    assert second_switch is not None, profiles.application(retry.id).status_reason
    assert switch.tick()
    second_job = _child_operation_id(switch.get(second_switch))
    assert second_job is not None
    with sessions() as session:
        retried_nodes = set(
            session.scalars(
                select(AgentOperation.node_id).where(
                    AgentOperation.parent_job_id == second_job
                )
            )
        )
    assert retried_nodes == set(nodes[1:] if first_node_removed else nodes)
    for node_id in retried_nodes:
        operations.record_node_result(
            second_job,
            node_id,
            succeeded=True,
            evidence={},
        )
    for _ in range(4):
        if switch.get(second_switch).state == "succeeded":
            break
        switch.tick()
    assert switch.get(second_switch).state == "succeeded"
    assert profiles.tick()
    final = profiles.application(retry.id)
    assert final.state == "succeeded"
    assert (
        profiles.load(
            profile.number,
            request_key=request_key,
            actor="admin",
        )
        == final
    )
    with sessions() as session:
        assert (
            _required(session.get(RecipeInstallation, installation.owner_id)).state
            == "uninstalled"
        )
        assert set(
            session.scalars(
                select(InstallationNode.state).where(
                    InstallationNode.installation_id == installation.owner_id
                )
            )
        ) == {"uninstalled"}


def test_uninstall_replans_a_stale_review_from_the_installation_as_it_is_now(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="8" * 35 + "e"
    )
    stale = service.preview_uninstall(installation.owner_id)
    with sessions.begin() as session:
        node = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation.owner_id,
                InstallationNode.node_id == nodes[1],
            )
        )
        assert node is not None
        node.installed_bytes += 1
    current = service.preview_uninstall(installation.owner_id).plan_digest
    assert current != stale.plan_digest

    # Uninstall is declarative: the stale review is re-planned, never refused.
    operation = service.uninstall(
        installation.owner_id,
        actor="admin",
        request_id="8" * 35 + "f",
    )
    replay = service.uninstall(
        installation.owner_id,
        actor="admin",
        request_id="8" * 35 + "f",
    )
    assert replay == operation
    with sessions() as session:
        assert _required(session.get(Job, operation.id)).payload["plan_digest"] == (
            current
        )
        children = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == operation.id)
                .order_by(AgentOperation.node_id)
            )
        )
        revision = session.get(
            CatalogDocumentRevision,
            _required(
                session.get(RecipeInstallation, installation.owner_id)
            ).recipe_revision_id,
        )
        assert len(children) == 2
        assert revision is not None
        assert {child.authority_revision for child in children} == {
            revision.content_digest
        }
        assert {child.payload["plan_digest"] for child in children} == {
            installation.plan_digest
        }
    for node_id in nodes:
        service.record_node_result(operation.id, node_id, succeeded=True, evidence={})
    fresh = service.preview_install(mapping_id, build_id)
    accepted = service.install(
        fresh,
        plan_digest=fresh.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    assert accepted.owner_id != installation.owner_id


def test_uninstall_queue_rollback_and_request_key_are_owner_bound(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    first = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="9" * 35 + "a"
    )
    second = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="9" * 35 + "b"
    )
    service._agent_jobs = FailingQueue()

    with pytest.raises(Exception) as _ending:
        service.uninstall(
            first.owner_id,
            actor="admin",
            request_id="9" * 35 + "c",
        )
    with sessions() as session:
        assert (
            _required(session.get(RecipeInstallation, first.owner_id)).state
            == "installed"
        )
        assert (
            session.scalar(select(Job).where(Job.request_id == "9" * 35 + "c")) is None
        )

    service._agent_jobs = RecordingQueue()
    operation = service.uninstall(
        first.owner_id,
        actor="admin",
        request_id="9" * 35 + "d",
    )
    with pytest.raises(Exception) as _ending:
        service.uninstall(
            second.owner_id,
            actor="admin",
            request_id="9" * 35 + "d",
        )
    assert operation.owner_id == first.owner_id


@pytest.mark.parametrize(
    ("uninstall_state", "blocked"),
    (("queued", True), ("running", True), ("failed", False), ("succeeded", False)),
)
def test_start_fences_only_active_uninstall_operations_after_installation_lock(
    tmp_path: Path, uninstall_state: str, blocked: bool
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="9" * 35 + "e"
    )
    run_plan = service.preview_run(
        installation.owner_id, "fenced" if blocked else "allowed"
    )
    uninstall = service.uninstall(
        installation.owner_id,
        actor="admin",
        request_id="9" * 35 + "f",
    )
    with sessions.begin() as session:
        job = session.get(Job, uninstall.id)
        assert job is not None
        job.state = uninstall_state

    if blocked:
        with pytest.raises(Exception) as _ending:
            service.start(
                run_plan,
                plan_digest=run_plan.plan_digest,
                actor="admin",
                request_id="9" * 35 + "0",
            )
    else:
        started = service.start(
            run_plan,
            plan_digest=run_plan.plan_digest,
            actor="admin",
            request_id="9" * 35 + "0",
        )
        assert started.kind == "recipe.start"

    with sessions() as session:
        start_jobs = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.start"))
        )
        runs = tuple(session.scalars(select(RecipeRun)))
    assert len(start_jobs) == len(runs) == (0 if blocked else 1)


def test_fresh_uninstall_request_supersedes_prior_cleanup_without_blocking(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="a" * 35 + "0"
    )
    first = service.uninstall(
        installation.owner_id,
        actor="admin",
        request_id="a" * 35 + "1",
    )

    fresh_plan = service.preview_uninstall(installation.owner_id)
    assert fresh_plan.allowed
    fresh = service.uninstall(
        installation.owner_id,
        actor="admin",
        request_id="a" * 35 + "2",
    )
    assert fresh.id != first.id

    with sessions() as session:
        parents = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.uninstall"))
        )
        children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == first.id)
            )
        )
    assert {parent.id for parent in parents} == {first.id, fresh.id}
    assert service.get(first.id).state == LifecycleState.CANCELLED
    assert service.get(fresh.id).state == LifecycleState.RUNNING
    assert {child.node_id for child in children} == set(nodes)

    from .non_blocking import assert_ended_without_blocking

    def next_request(_world):
        next_plan = service.preview_uninstall(installation.owner_id)
        assert next_plan.allowed
        return service.uninstall(
            installation.owner_id,
            actor="admin",
            request_id="a" * 35 + "3",
        )

    def request_key(receipt):
        with sessions() as session:
            parent = session.get(Job, receipt.id)
            assert parent is not None
            return parent.request_id

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        first,
        end=lambda _receipt: service.get(first.id),
        fresh=next_request,
        request_key=request_key,
    )


def test_run_status_projects_exact_rank_health_without_agent_secrets(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="9" * 36,
    )
    for node_id in nodes:
        service.record_node_result(
            install.id, node_id, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "qwen")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="a" * 36,
    )
    with sessions.begin() as session:
        ranks = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == start.owner_id)
                .order_by(RunNode.rank)
            )
        )
        ranks[0].state = "running"
        ranks[0].updated_at = NOW
        ranks[1].state = "failed"
        ranks[1].updated_at = NOW

    status = service.run_status(start.owner_id)

    assert status.id == start.owner_id
    assert status.alias == "qwen"
    assert status.healthy is False
    assert [rank.state for rank in status.ranks] == ["running", "failed"]
    assert [rank.fresh for rank in status.ranks] == [True, True]
    assert [rank.age_seconds for rank in status.ranks] == [0.0, 0.0]
    with sessions() as session:
        assert all(
            _required(session.get(AgentNode, node_id)).state == "active"
            for node_id in nodes
        )

    with sessions.begin() as session:
        exact_run = _required(session.get(RecipeRun, start.owner_id))
        exact_worker = _required(
            session.scalar(
                select(RunNode).where(
                    RunNode.run_id == start.owner_id,
                    RunNode.node_id == nodes[1],
                )
            )
        )
        exact_run.state = "starting"
        exact_worker.state = "running"
        exact_worker.updated_at = NOW + timedelta(seconds=4)
        # An empty report never fails a rank of a run that is not running.
        prepare_exact_recipe_run_observation_nodes(
            session, nodes[1], NOW + timedelta(seconds=5), set()
        )
    with sessions() as session:
        exact_worker = _required(
            session.scalar(
                select(RunNode).where(
                    RunNode.run_id == start.owner_id,
                    RunNode.node_id == nodes[1],
                )
            )
        )
        assert exact_worker.state == "running"


def test_distributed_recovery_launches_all_ranks_before_collective_readiness(
    tmp_path: Path,
) -> None:
    (
        sessions,
        service,
        _routes,
        _publisher,
        _started,
        restart,
        _worker_start,
        _nodes,
    ) = _queued_distributed_recovery_restart(tmp_path)
    with sessions() as session:
        launches = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == restart.id)
                .order_by(AgentOperation.node_id)
            )
        )
    assert len(launches) == 2
    assert {item.payload["phase"] for item in launches} == {"rank-launch"}

    service.record_node_result(
        restart.id,
        launches[0].node_id,
        succeeded=True,
        evidence=start_evidence(launches[0].payload),
    )
    with sessions() as session:
        assert (
            session.query(AgentOperation).filter_by(parent_job_id=restart.id).count()
            == 2
        )

    service.record_node_result(
        restart.id,
        launches[1].node_id,
        succeeded=True,
        evidence=start_evidence(launches[1].payload),
    )
    with sessions() as session:
        operations = tuple(
            session.query(AgentOperation).filter_by(parent_job_id=restart.id)
        )
        readiness = tuple(
            item
            for item in operations
            if item.payload.get("phase") == "collective-readiness"
        )
        assert len(operations) == 3
        assert len(readiness) == 1
        assert _placement(readiness[0].payload)["role"] == "entrypoint"


def test_distributed_recovery_deadline_is_enforced_during_stop_phase_advance(
    tmp_path: Path,
) -> None:
    (
        sessions,
        service,
        _routes,
        _publisher,
        started,
        stop_job,
        nodes,
    ) = _queued_distributed_recovery_stop(tmp_path)
    service._clock = lambda: _recovery_deadline(stop_job)

    service.record_node_result(stop_job.id, nodes[0], succeeded=True, evidence={})

    with sessions() as session:
        worker_stop = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == stop_job.id,
                AgentOperation.node_id == nodes[1],
            )
        )
        run = _required(session.get(RecipeRun, started.owner_id))
        stored_stop = _required(session.get(Job, stop_job.id))
        assert worker_stop is None
        assert stored_stop.state == "failed"
        assert run.state == "lost"  # the second rank was never stopped
        assert run.route_state == "withdrawn"


def test_distributed_recovery_deadline_is_enforced_before_phase_advance(
    tmp_path: Path,
) -> None:
    (
        sessions,
        service,
        _routes,
        _publisher,
        started,
        restart,
        worker_start,
        nodes,
    ) = _queued_distributed_recovery_restart(tmp_path)
    service._clock = lambda: _recovery_deadline(restart)

    service.record_node_result(
        restart.id,
        nodes[1],
        succeeded=True,
        evidence=start_evidence(worker_start.payload),
    )

    with sessions() as session:
        owner_start = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == restart.id,
                AgentOperation.node_id == nodes[0],
            )
        )
        run = _required(session.get(RecipeRun, started.owner_id))
        stored_restart = _required(session.get(Job, restart.id))
        assert owner_start is not None
        assert owner_start.state == "failed"
        assert stored_restart.state == "failed"
        assert run.state == "stopping"
        assert run.route_state == "withdrawn"


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("missing_receipt", [False, True])
def test_distributed_recovery_deadline_ends_attempt_and_retries_publication(
    tmp_path: Path,
    missing_receipt: bool,
) -> None:
    (
        sessions,
        service,
        routes,
        publisher,
        started,
        restart,
        worker_start,
        nodes,
    ) = _queued_distributed_recovery_restart(tmp_path)
    service.record_node_result(
        restart.id,
        nodes[1],
        succeeded=True,
        evidence=start_evidence(worker_start.payload),
    )
    with sessions() as session:
        owner_start = _required(
            session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == restart.id,
                    AgentOperation.node_id == nodes[0],
                )
            )
        )
    service.record_node_result(
        restart.id,
        nodes[0],
        succeeded=True,
        evidence=start_evidence(owner_start.payload),
    )
    with sessions() as session:
        readiness = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == restart.id,
                AgentOperation.node_id == nodes[0],
                AgentOperation.state == "queued",
            )
        )
    assert readiness is not None
    service.record_node_result(
        restart.id,
        nodes[0],
        succeeded=True,
        evidence=start_evidence(readiness.payload),
    )
    if missing_receipt:
        with sessions.begin() as session:
            recovery = _required(session.get(Job, restart.id))
            recovery.result = None
    routes._clock = lambda: _recovery_deadline(restart)
    # First publication still needs current rank evidence; only the recovery
    # attempt's bookkeeping deadline has expired.
    mark_current_exact_observations(sessions, started.owner_id, routes._clock())
    publications_before = len(publisher.aliases)

    # The pass that meets the expired deadline ends the attempt; the next
    # pass (the worker's durable retry) publishes.
    with pytest.raises(RecipeRecoveryDeadlineError):
        routes.publish_run(started.owner_id)
    assert len(publisher.aliases) == publications_before
    generation = routes.publish_run(started.owner_id)

    assert len(publisher.aliases) == publications_before + 1
    with sessions() as session:
        run = _required(session.get(RecipeRun, started.owner_id))
        assert run.state == RunState.RUNNING
        assert run.route_state == RouteState.PUBLISHED
        assert routes._recovery_job(session, run) is None
    # The expired request leaves no gate on a fresh authorized publication.
    fresh = routes.publish_run(started.owner_id)
    assert fresh.generation > generation.generation
    with sessions() as session:
        run = _required(session.get(RecipeRun, started.owner_id))
        assert run.route_state == RouteState.PUBLISHED
        assert run.route_generation == fresh.generation


def test_recovery_phase_deadline_is_resampled_after_waiting_for_job_lock(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    (
        sessions,
        service,
        _routes,
        _publisher,
        started,
        restart,
        worker_start,
        nodes,
    ) = _queued_distributed_recovery_restart(tmp_path, engine=postgres_engine)
    current = {"now": NOW}
    service._clock = lambda: current["now"]
    worker_pid: dict[str, int] = {}
    lock_started = threading.Event()

    def before_lock(connection, _cursor, statement, _parameters, _context, _many):
        if "FROM jobs" in statement and "FOR UPDATE" in statement:
            worker_pid["value"] = _postgres_backend_pid(connection)
            lock_started.set()

    blocker = postgres_engine.connect()
    transaction = blocker.begin()
    blocker_pid = _postgres_backend_pid(blocker)
    blocker.execute(select(Job).where(Job.id == restart.id).with_for_update())
    event.listen(postgres_engine, "before_cursor_execute", before_lock)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        result = pool.submit(
            service.record_node_result,
            restart.id,
            nodes[1],
            succeeded=True,
            evidence=start_evidence(worker_start.payload),
        )
        assert lock_started.wait(timeout=10)
        _wait_for_postgres_block(
            postgres_engine,
            blocked_pid=worker_pid["value"],
            blocker_pid=blocker_pid,
        )
        current["now"] = _recovery_deadline(restart)
        transaction.commit()
        result.result(timeout=10)
    finally:
        if transaction.is_active:
            transaction.rollback()
        blocker.close()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", before_lock)

    with sessions() as session:
        owner_start = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == restart.id,
                AgentOperation.node_id == nodes[0],
            )
        )
        assert owner_start is not None
        assert owner_start.state == "failed"
        assert session.get(Job, restart.id).state == "failed"  # type: ignore[union-attr]
        assert session.get(RecipeRun, started.owner_id).state == "stopping"  # type: ignore[union-attr]
