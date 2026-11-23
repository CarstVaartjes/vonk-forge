"""Recipe operations: recovery multinode."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol.contracts import canonical_message
from vonk_agent_protocol.outcome import UnknownError
from vonk_agent_protocol.recipe_operations import RecipeStopPayload
from vonk_agent_protocol.route_activation import SupervisorPhase
from vonk_agent_protocol.state_machines import (
    RoutePublicationState,
    RouteState,
    RunState,
)
from vonk_control.bounded_json import require_mapping
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.distributed_recovery.coordinator import DistributedRecoveryCoordinator
from vonk_control.gateway.litellm import ServedModelInfo
from vonk_control.gateway.route_runtime import (
    RECIPE_ROUTE_AUTHORITY_ID,
    AtomicRouteBundlePublisher,
    FileSupervisorAcknowledger,
    verify_active_route_bundle,
)
from vonk_control.jobs.documents import RecipeStopParent
from vonk_control.model_primitives import Base
from vonk_control.models.execution import (
    RecipeRun,
    RunNode,
)
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.mapping import ClusterMapping
from vonk_control.models.operations import AgentOperation
from vonk_control.models.routes import RoutePublication
from vonk_control.recipe_operations.lifecycle_contract import (
    RecipeOperationResult,
)
from vonk_control.recipe_routes.publisher import AtomicRecipeRoutePublisher

from .agent_fences import fenced_operation
from .recipe_operations_support import (
    NOW,
    ConcurrentPublisher,
    _issue_exact_stop_grant,
    _job_phases,
    _placement,
    _queued_distributed_recovery_restart,
    _queued_distributed_recovery_stop,
    _recovery_deadline,
    _required,
    _typed_stop_payloads,
    bind_route_publications,
    complete_collective_readiness,
    installed_recipe,
    mark_current_exact_observations,
    record_exact_empty_snapshot,
    setup_services,
    start_evidence,
    started_recipe,
)


def test_recovery_publication_crossing_deadline_retains_acknowledged_route(
    tmp_path: Path,
) -> None:
    (
        sessions,
        service,
        routes,
        _publisher,
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
    complete_collective_readiness(sessions, service, restart.id, nodes[0])
    current = {"now": NOW}

    class DeadlineCrossingPublisher(ConcurrentPublisher):
        def publish_recipe(
            self, candidate, *, required: SupervisorPhase = SupervisorPhase.READY
        ):
            generation = super().publish_recipe(candidate, required=required)
            current["now"] = _recovery_deadline(restart)
            return generation

    publisher = DeadlineCrossingPublisher()
    routes._publisher = publisher
    routes._clock = lambda: current["now"]

    generation = routes.publish_run(started.owner_id)

    assert publisher.aliases == [("deadline-gang",)]
    with sessions() as session:
        run = _required(session.get(RecipeRun, started.owner_id))
        recovery = _required(session.get(Job, restart.id))
        assert run.state == RunState.RUNNING
        assert run.route_state == RouteState.PUBLISHED
        assert run.route_generation == generation.generation
        assert routes._recovery_job(session, run) is None
        recovery_result = RecipeOperationResult.model_validate_json(
            json.dumps(recovery.result)
        )
        assert recovery_result.recovery_error is not None
        assert recovery_result.recovery_route_published is None
    fresh = routes.publish_run(started.owner_id)
    assert fresh.generation > generation.generation
    assert publisher.aliases == [("deadline-gang",), ("deadline-gang",)]


def test_expired_recovery_route_never_needs_compensating_withdrawal(
    tmp_path: Path,
) -> None:
    (
        sessions,
        service,
        routes,
        _publisher,
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
    complete_collective_readiness(sessions, service, restart.id, nodes[0])

    current = {"now": NOW}
    live_root = tmp_path / "live-routes"
    runtime = AtomicRouteBundlePublisher(live_root)
    atomic = AtomicRecipeRoutePublisher(runtime)

    class DeadlineCrossingWithdrawalFailure(AtomicRecipeRoutePublisher):
        def __init__(self) -> None:
            self.withdrawal_attempts = 0
            self.fail_withdrawal = True

        def served_model_info(self):
            return ServedModelInfo()

        def publish_recipe(
            self, candidate, *, required: SupervisorPhase = SupervisorPhase.READY
        ):
            generation = atomic.publish_recipe(candidate, required=required)
            current["now"] = _recovery_deadline(restart)
            return generation

        def publish_empty(self, route_digest):
            self.withdrawal_attempts += 1
            if self.fail_withdrawal:
                raise RuntimeError("synthetic route withdrawal failure")
            return atomic.publish_empty(route_digest)

    failing = DeadlineCrossingWithdrawalFailure()
    routes._publisher = failing
    routes._clock = lambda: current["now"]

    generation = routes.publish_run(started.owner_id)

    with sessions() as session:
        run = _required(session.get(RecipeRun, started.owner_id))
        recovery = _required(session.get(Job, restart.id))
        publication = session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
        assert run.state == RunState.RUNNING
        assert run.route_state == RouteState.PUBLISHED
        assert routes._recovery_job(session, run) is None
        recovery_result = RecipeOperationResult.model_validate_json(
            json.dumps(recovery.result)
        )
        assert recovery_result.recovery_error is not None
        assert recovery_result.recovery_route_published is None
        assert publication is not None
        assert publication.state == RoutePublicationState.COMPLETED
    assert failing.withdrawal_attempts == 0
    verified = verify_active_route_bundle(live_root)
    assert not isinstance(verified, UnknownError)
    assert verified.routes is not None
    assert "deadline-gang" in verified.routes.routes
    fresh = routes.publish_run(started.owner_id)
    assert fresh.generation == generation.generation
    assert failing.withdrawal_attempts == 0


def test_recovery_expiry_inside_real_supervisor_ack_retains_live_route(
    tmp_path: Path,
) -> None:
    (
        sessions,
        service,
        routes,
        _publisher,
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
    complete_collective_readiness(sessions, service, restart.id, nodes[0])

    current = {"now": _recovery_deadline(restart) - timedelta(seconds=1)}
    mark_current_exact_observations(sessions, started.owner_id, current["now"])
    live_root = tmp_path / "ack-routes"
    ack_path = tmp_path / "supervisor/ack.json"
    ack_path.parent.mkdir()

    def expire_while_waiting_for_ack(marker, required) -> None:
        acknowledgement = {
            "acknowledged_at": current["now"].isoformat(),
            "activation_sha256": marker.digest,
            "child_pid": 4321,
            "generation": marker.generation,
            "litellm_sha256": marker.litellm_sha256,
            "schema_version": 1,
            "state": marker.state,
        }
        acknowledgement_bytes = (
            json.dumps(acknowledgement, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()

        def acknowledge_at_exact_deadline(_seconds: float) -> None:
            ack_path.write_bytes(acknowledgement_bytes)
            current["now"] = _recovery_deadline(restart)

        moments = iter((0.0, 0.1))
        FileSupervisorAcknowledger(
            ack_path,
            clock=lambda: current["now"],
            timeout_seconds=0.5,
            poll_seconds=0.1,
            monotonic=lambda: next(moments),
            sleep=acknowledge_at_exact_deadline,
        )(marker, required)

    runtime = AtomicRouteBundlePublisher(
        live_root,
        await_supervisor_ack=expire_while_waiting_for_ack,
    )
    atomic = AtomicRecipeRoutePublisher(runtime)

    class AcknowledgementCrossingWithdrawalFailure(AtomicRecipeRoutePublisher):
        def __init__(self) -> None:
            self.withdrawal_attempts = 0
            self.fail_withdrawal = True

        def served_model_info(self):
            return ServedModelInfo()

        def publish_recipe(
            self, candidate, *, required: SupervisorPhase = SupervisorPhase.READY
        ):
            return atomic.publish_recipe(candidate, required=required)

        def publish_empty(self, route_digest):
            self.withdrawal_attempts += 1
            if self.fail_withdrawal:
                raise RuntimeError("synthetic route withdrawal failure")
            return atomic.publish_empty(route_digest)

    failing = AcknowledgementCrossingWithdrawalFailure()
    routes._publisher = failing
    routes._clock = lambda: current["now"]

    generation = routes.publish_run(started.owner_id)

    with sessions() as session:
        run = _required(session.get(RecipeRun, started.owner_id))
        recovery = _required(session.get(Job, restart.id))
        publication = session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
        assert run.state == RunState.RUNNING
        assert run.route_state == RouteState.PUBLISHED
        assert routes._recovery_job(session, run) is None
        recovery_result = RecipeOperationResult.model_validate_json(
            json.dumps(recovery.result)
        )
        assert recovery_result.recovery_error is not None
        assert recovery_result.recovery_route_published is None
        assert publication is not None
        assert publication.state == RoutePublicationState.COMPLETED
    assert failing.withdrawal_attempts == 0
    verified = verify_active_route_bundle(live_root)
    assert not isinstance(verified, UnknownError)
    assert verified.routes is not None
    assert "deadline-gang" in verified.routes.routes
    runtime._await_supervisor_ack = None
    fresh = routes.publish_run(started.owner_id)
    assert fresh.generation == generation.generation
    assert failing.withdrawal_attempts == 0


def test_distributed_rank_loss_waits_for_fresh_presence_when_evidence_is_missing(
    tmp_path: Path,
) -> None:
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="m" * 36
    )
    start = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="n" * 36,
        alias="failed-authority-gang",
    )
    publisher = ConcurrentPublisher()
    _service, routes = bind_route_publications(sessions, service, publisher)
    routes.publish_run(start.owner_id)
    failure_observed_at = NOW + timedelta(seconds=1)
    record_exact_empty_snapshot(sessions, nodes[1], failure_observed_at)
    with sessions.begin() as session:
        for presence in session.scalars(select(AgentNode)):
            session.delete(presence)
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: NOW
    )

    assert recovery.tick() is True

    with sessions() as session:
        run = _required(session.get(RecipeRun, start.owner_id))
        # Missing presence evidence waits like the singleton path instead of
        # replaying stale authority, and the route stays withdrawn meanwhile.
        assert run.state == "running"
        assert run.route_state == "withdrawn"
        assert run.route_error is not None
        assert "fresh Controller-observed Spark presence" in run.route_error
        assert run.run_generation == 1
        assert not session.scalar(
            select(Job.id).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == start.owner_id,
            )
        )
    assert publisher.aliases[-1] == ()


def test_multinode_start_is_bound_to_authenticated_fabric_rendezvous(
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
        request_id="d" * 36,
    )
    for node in nodes:
        service.record_node_result(
            install.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "qwen-gang")
    assert run_plan.allowed is True
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="e" * 36,
    )

    with sessions() as session:
        job = _required(session.get(Job, start.id))
        children = [
            _placement(entry["payload"])
            for phase in _job_phases(job.payload)
            for entry in phase
            if require_mapping(entry["payload"], "phase payload")["phase"]
            == "rank-launch"
        ]
        assert [child["local_address"] for child in children] == [
            "192.168.100.3",
            "192.168.100.2",
        ]
        assert {child["master_address"] for child in children} == {"192.168.100.2"}
        assert {child["master_port"] for child in children} == {29500}
        assert {child["world_size"] for child in children} == {2}
        assert [child["endpoint_address"] for child in children] == [
            None,
            "192.168.1.211",
        ]


def test_multinode_worker_endpoint_is_never_published_on_management_lan(
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
        request_id="a" * 35 + "1",
    )
    for node in nodes:
        service.record_node_result(
            install.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )

    run_plan = service.preview_run(install.owner_id, "qwen-gang")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="a" * 35 + "2",
    )

    with sessions() as session:
        job = _required(session.get(Job, start.id))
        children = {
            entry["node_id"]: _placement(entry["payload"])
            for phase in _job_phases(job.payload)
            for entry in phase
        }
    assert children[nodes[0]]["endpoint_address"] == "192.168.1.211"
    assert children[nodes[1]]["endpoint_address"] is None
    assert children[nodes[1]]["local_address"] == "192.168.100.3"


def test_failed_multinode_start_queues_idempotent_stop_for_every_rank(
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
        request_id="f" * 35 + "0",
    )
    for node in nodes:
        service.record_node_result(
            install.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "qwen-gang")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="f" * 35 + "1",
    )
    with sessions() as session:
        worker = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
        )
        assert worker is not None
    service.record_node_result(
        start.id, worker.node_id, succeeded=False, evidence={"code": "start.failed"}
    )
    # Every rank launches in one phase, so cleanup waits for the other report.
    other = next(node for node in nodes if node != worker.node_id)
    service.record_node_result(
        start.id, other, succeeded=False, evidence={"code": "start.failed"}
    )
    # A duplicate final failure report must reuse the one deterministic gang
    # cleanup request instead of creating another stop operation.
    service.record_node_result(
        start.id, worker.node_id, succeeded=False, evidence={"code": "start.failed"}
    )

    with sessions() as session:
        cleanups = tuple(
            session.scalars(
                select(Job).where(
                    Job.kind == "recipe.stop",
                    Job.payload["owner_id"].as_string() == start.owner_id,
                )
            )
        )
        assert len(cleanups) == 1
        cleanup = cleanups[0]
        assert set(cleanup.targets) == set(nodes)
        stop_payloads = _typed_stop_payloads(cleanup.payload)
        assert len(stop_payloads) == len(nodes)
        assert {stop.run_id for stop in stop_payloads} == {start.owner_id}
        assert {stop.target_runtime_id for stop in stop_payloads} == {start.owner_id}
        assert {stop.run_generation for stop in stop_payloads} == {1}
        assert all(stop.cancel_pending_start for stop in stop_payloads)
        cleanup_children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == cleanup.id)
            )
        )
        assert [child.node_id for child in cleanup_children] == [nodes[0]]
        cleanup_payload = RecipeStopPayload.model_validate_json(
            canonical_message(cleanup_children[0].payload), strict=True
        )
        assert cleanup_payload.run_id == start.owner_id
        assert cleanup_payload.target_runtime_id == start.owner_id
        assert cleanup_payload.plan_digest == start.plan_digest
        assert cleanup_payload.run_generation == 1
        assert cleanup_payload.cancel_pending_start is True
        assert _required(session.get(RecipeRun, start.owner_id)).state == "stopping"


@pytest.mark.parametrize("newer_intent", ["none", "all-nodes", "one-node"])
def test_every_failed_start_leaves_what_it_launched_stoppable_or_queues_its_stop(
    tmp_path: Path, newer_intent: str
) -> None:
    """A Start that ended failed may have launched ranks. Whatever intent owns the
    Sparks by then, the run is never left ``failed`` (which nothing stops): a stop
    is queued (under the intent that owns the Sparks now, never taking a new one),
    or, with no single owner, the run stays stoppable for its owner."""

    from vonk_control.models.execution import STOPPABLE_RUN_STATES

    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="f" * 35 + "0",
    )
    for node in nodes:
        service.record_node_result(
            install.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "qwen-gang")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="f" * 35 + "1",
    )
    with sessions.begin() as session:
        agent_nodes = tuple(
            session.scalars(select(AgentNode).order_by(AgentNode.node_id))
        )
        bound = agent_nodes[0].workload_intent_ordinal
        if newer_intent == "all-nodes":
            for node in agent_nodes:
                node.workload_intent_ordinal = bound + 1
        elif newer_intent == "one-node":
            agent_nodes[0].workload_intent_ordinal = bound + 1
        owner_ordinals = {node.workload_intent_ordinal for node in agent_nodes}
    for node in nodes:
        service.record_node_result(
            start.id, node, succeeded=False, evidence={"code": "start.failed"}
        )

    with sessions() as session:
        run = _required(session.get(RecipeRun, start.owner_id))
        cleanups = tuple(
            session.scalars(
                select(Job).where(
                    Job.kind == "recipe.stop",
                    Job.payload["owner_id"].as_string() == start.owner_id,
                )
            )
        )
        assert run.state in STOPPABLE_RUN_STATES, run.state
        if newer_intent == "one-node":
            # No single intent owns both Sparks: nothing may take authority over
            # either, and the run stays stoppable for whoever does.
            assert cleanups == () and run.state == "lost"
        else:
            assert len(cleanups) == 1 and run.state == "stopping"
            assert (
                cleanups[0].payload["workload_intent_ordinal"] == owner_ordinals.pop()
            )
            # The cleanup joined the owning intent: no Spark moved to a new one.
            assert {
                node.workload_intent_ordinal
                for node in session.scalars(select(AgentNode))
            } == ({bound} if newer_intent == "none" else {bound + 1})


def test_postgres_ordinary_stop_grant_uses_the_queued_run_plan(
    tmp_path: Path, postgres_engine
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="g" * 36
    )
    started = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="h" * 36,
        alias="authority-stop",
    )
    plan = service.preview_stop(started.owner_id)
    stopped = service.stop(
        started.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="i" * 36,
    )
    with sessions() as session:
        parent = _required(session.get(Job, stopped.id))
        run = _required(session.get(RecipeRun, started.owner_id))
        assert parent.payload["plan_digest"] == plan.plan_digest
        assert parent.payload["plan_digest"] != run.plan_digest
        assert parent.authority_revision == run.plan_digest.removeprefix("sha256:")

    claim, payload, _grant = _issue_exact_stop_grant(
        sessions,
        node_id=nodes[0],
        certificate_serial="serial-0",
    )

    with sessions() as session:
        parent = _required(session.get(Job, stopped.id))
        run = _required(session.get(RecipeRun, started.owner_id))
        assert fenced_operation(sessions, claim).parent_job_id == parent.id
        assert payload.run_id == run.id
        assert parent.payload["plan_digest"] == plan.plan_digest
        assert parent.authority_revision == run.plan_digest.removeprefix("sha256:")


def test_postgres_failed_start_cleanup_stop_grant_uses_the_queued_run_plan(
    tmp_path: Path, postgres_engine
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="j" * 36
    )
    plan = service.preview_run(installation.owner_id, "failed-start-cleanup")
    started = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="k" * 36,
    )
    service.record_node_result(
        started.id, nodes[0], succeeded=False, evidence={"code": "start.failed"}
    )
    with sessions() as session:
        cleanup = _required(
            session.scalar(
                select(Job).where(
                    Job.kind == "recipe.stop",
                    Job.payload["owner_id"].as_string() == started.owner_id,
                )
            )
        )

    claim, payload, _grant = _issue_exact_stop_grant(
        sessions,
        node_id=nodes[0],
        certificate_serial="serial-0",
    )

    with sessions() as session:
        parent = _required(session.get(Job, cleanup.id))
        run = _required(session.get(RecipeRun, started.owner_id))
        assert fenced_operation(sessions, claim).parent_job_id == parent.id
        assert payload.run_id == run.id
        assert parent.authority_revision == run.plan_digest.removeprefix("sha256:")


def test_postgres_dual_recovery_stop_grant_uses_the_queued_run_plan(
    tmp_path: Path, postgres_engine
) -> None:
    sessions, _service, _routes, _publisher, started, stop, nodes = (
        _queued_distributed_recovery_stop(tmp_path, engine=postgres_engine)
    )

    claim, payload, _grant = _issue_exact_stop_grant(
        sessions,
        node_id=nodes[0],
        certificate_serial="serial-0",
    )

    with sessions() as session:
        parent = _required(session.get(Job, stop.id))
        run = _required(session.get(RecipeRun, started.owner_id))
        assert fenced_operation(sessions, claim).parent_job_id == parent.id
        assert payload.run_id == run.id
        assert payload.run_generation + 1 == run.run_generation
        assert parent.actor == "system:distributed-recovery"
        assert parent.authority_revision == run.plan_digest.removeprefix("sha256:")


def test_concurrent_final_rank_results_serialize_gang_cleanup(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=3, engine=postgres_engine
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="d" * 36,
    )
    for node in nodes:
        service.record_node_result(
            install.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "qwen-gang")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="e" * 36,
    )
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == start.id,
                AgentOperation.node_id == nodes[1],
            )
        )
        assert child is not None
        evidence = start_evidence(child.payload)
    # Every rank launches in one phase; the last two results race.
    service.record_node_result(start.id, nodes[0], succeeded=True, evidence={})
    barrier = threading.Barrier(2)

    def result(node_id: str, succeeded: bool) -> None:
        barrier.wait()
        service.record_node_result(
            start.id,
            node_id,
            succeeded=succeeded,
            evidence=evidence if succeeded else {"code": "start.failed"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(result, nodes[1], True),
            pool.submit(result, nodes[2], False),
        ]
        for future in futures:
            future.result(timeout=10)

    with sessions() as session:
        cleanups = tuple(
            session.scalars(
                select(Job).where(
                    Job.kind == "recipe.stop",
                    Job.payload["owner_id"].as_string() == start.owner_id,
                )
            )
        )
        assert len(cleanups) == 1
        cleanup = cleanups[0]
        assert _required(session.get(RecipeRun, start.owner_id)).state == "stopping"
        assert set(cleanup.targets) == set(nodes)
        stop_payloads = _typed_stop_payloads(cleanup.payload)
        assert len(stop_payloads) == len(nodes)
        assert {stop.run_id for stop in stop_payloads} == {start.owner_id}
        assert {stop.target_runtime_id for stop in stop_payloads} == {start.owner_id}
        assert {stop.run_generation for stop in stop_payloads} == {1}
        assert all(stop.cancel_pending_start for stop in stop_payloads)


def test_postgres_disjoint_stops_serialize_one_route_candidate(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=4, mapping_node_count=2, engine=postgres_engine
    )
    first_nodes, second_nodes = tuple(nodes[:2]), tuple(nodes[2:])
    assert set(first_nodes).isdisjoint(second_nodes)
    installation = installed_recipe(
        service, mapping_id, build_id, first_nodes, request_id="e" * 35 + "1"
    )
    first = started_recipe(
        sessions,
        service,
        installation.owner_id,
        first_nodes,
        request_id="e" * 35 + "2",
        alias="first",
    )
    with sessions() as session:
        first_mapping = session.get(ClusterMapping, mapping_id)
        assert first_mapping is not None
        revision_id = first_mapping.recipe_revision_id
    mappings = ClusterMappingService(sessions)
    second_mapping = mappings.preview(revision_id, second_nodes, {}, "admin")
    second_mapping_id = mappings.materialize(second_mapping, actor="admin", now=NOW)
    second_installation = installed_recipe(
        service, second_mapping_id, build_id, second_nodes, request_id="e" * 35 + "5"
    )
    second = started_recipe(
        sessions,
        service,
        second_installation.owner_id,
        second_nodes,
        request_id="e" * 35 + "6",
        alias="second",
    )
    second_run_id = second.owner_id
    expected_members = {first.owner_id: first_nodes, second_run_id: second_nodes}
    mark_current_exact_observations(sessions, first.owner_id, NOW)
    mark_current_exact_observations(sessions, second_run_id, NOW)
    publisher = ConcurrentPublisher()
    service, routes = bind_route_publications(sessions, service, publisher)
    routes.publish_run(first.owner_id)
    routes.publish_run(second_run_id)
    plans = {
        first.owner_id: service.preview_stop(first.owner_id),
        second_run_id: service.preview_stop(second_run_id),
    }
    start = threading.Barrier(2)

    def stop(item: tuple[str, str]) -> str:
        run_id, request_key = item
        start.wait()
        return service.stop(
            run_id,
            plan_digest=plans[run_id].plan_digest,
            actor="admin",
            request_id=request_key,
        ).id

    requests = (
        (first.owner_id, "e" * 35 + "3"),
        (second_run_id, "e" * 35 + "4"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        operation_ids = list(pool.map(stop, requests))
    # A Stop that found the route owner held by the other one waits, accepted;
    # its own continuation withdraws and dispatches it once the owner is free.
    for turn in range(1, 5):
        with sessions() as session:
            if all(
                _required(session.get(RecipeRun, run_id)).state == "stopping"
                for run_id, _request_key in requests
            ):
                break
        service._clock = lambda turn=turn: NOW + timedelta(seconds=10 * turn)
        service.reconcile_pending_service_stops()

    assert len(set(operation_ids)) == 2
    assert publisher.aliases[-1] == ()
    with sessions() as session:
        stop_jobs = tuple(
            session.scalars(
                select(Job).where(
                    Job.kind == "recipe.stop",
                    Job.payload["owner_id"]
                    .as_string()
                    .in_((first.owner_id, second_run_id)),
                )
            )
        )
        assert len(stop_jobs) == 2
        for stop_job in stop_jobs:
            stop_payloads = _typed_stop_payloads(stop_job.payload)
            stop_parent = RecipeStopParent.model_validate_json(
                canonical_message(stop_job.payload), strict=True
            )
            run_id = stop_parent.owner_id
            assert len(stop_payloads) == len(expected_members[run_id])
            assert tuple(stop_job.targets) == expected_members[run_id]
            members = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            )
            assert [(row.node_id, row.rank, row.role) for row in members] == [
                (expected_members[run_id][0], 0, "entrypoint"),
                (expected_members[run_id][1], 1, "worker"),
            ]
            assert {
                row.workload_intent_ordinal
                for row in session.scalars(
                    select(AgentNode).where(
                        AgentNode.node_id.in_(expected_members[run_id])
                    )
                )
            } == {stop_job.payload["workload_intent_ordinal"]}
            retained_run = _required(session.get(RecipeRun, run_id))
            assert sorted((stop.rank, stop.role) for stop in stop_payloads) == [
                (0, "entrypoint"),
                (1, "worker"),
            ]
            assert {
                (stop.installation_id, stop.run_generation, stop.plan_digest)
                for stop in stop_payloads
            } == {
                (
                    retained_run.installation_id,
                    retained_run.run_generation,
                    retained_run.plan_digest,
                )
            }
            assert {stop.run_id for stop in stop_payloads} == {run_id}
            assert {stop.target_runtime_id for stop in stop_payloads} == {run_id}
            assert all(stop.cancel_pending_start for stop in stop_payloads)
        assert [
            (
                _required(session.get(RecipeRun, run_id)).state,
                _required(session.get(RecipeRun, run_id)).route_state,
            )
            for run_id, _request_key in requests
        ] == [("stopping", "withdrawn"), ("stopping", "withdrawn")]
    # Both accepted Stops end stopped, and the wait held nothing: a fresh
    # Start is admitted afterwards.
    for stop_job in stop_jobs:
        for node_id in stop_job.targets:
            service.record_node_result(
                stop_job.id, node_id, succeeded=True, evidence={}
            )
    with sessions() as session:
        assert {
            _required(session.get(RecipeRun, run_id)).state
            for run_id, _request_key in requests
        } == {"stopped"}
    fresh_plan = service.preview_run(installation.owner_id, "first")
    fresh = service.start(
        fresh_plan,
        plan_digest=fresh_plan.plan_digest,
        actor="admin",
        request_id="e" * 35 + "7",
    )
    assert fresh.state in {"queued", "running"}
