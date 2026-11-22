from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    ContainerRuntimeAction,
    ExecuteContainerRuntimeRequestOperation,
    LifecycleState,
    RecipeStopPayload,
    canonical_message,
    host_helper_grant_signing_bytes,
)
from vonk_agent_protocol.host_helper import HostRuntimeRequest
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_operation_facts import SUPERSEDED_CANCELLATION_SECONDS
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.host_helper_authority import (
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.host_runtime_plan_authority import derive_runtime_plan_binding
from vonk_control.inventory.items import inventory_items
from vonk_control.jobs.documents import RecipeStopParent
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Base,
    ClusterMapping,
    Job,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.recipe_operations import (
    RecipeOperationConflict,
)
from vonk_control.run_admission import RunAdmissionBusy

from .agent_fences import fenced_operation
from .recipe_operations_support import (
    NOW,
    ConcurrentPublisher,
    _postgres_backend_pid,
    _queued_distributed_recovery_stop,
    _required,
    _typed_stop_payloads,
    _wait_for_postgres_block,
    bind_route_publications,
    installed_recipe,
    mark_current_exact_observations,
    setup_services,
    start_evidence,
    started_recipe,
)
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    claim_agent,
)


def _issue_exact_stop_grant(
    sessions: sessionmaker[Session],
    *,
    node_id: str,
    certificate_serial: str,
    grant_now: datetime = NOW,
    lease_seconds: int = 600,
):
    """Claim a queued Stop and exercise the production exact-plan signer."""

    runtime_identity = {
        **PACKAGED_RUNTIME_IDENTITY,
        "architecture": "linux-arm64",
    }
    queue = AgentJobService(sessions, clock=lambda: grant_now)
    with sessions() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        fingerprint = inventory_items(node).preflight_fingerprint
    claim = claim_agent(
        queue,
        node_id,
        certificate_serial,
        runtime_identity=runtime_identity,
        preflight_fingerprint=fingerprint,
    )
    assert claim is not None and claim.operation.value == "recipe.stop"
    stop = RecipeStopPayload.model_validate_json(
        canonical_message(claim.payload), strict=True
    )
    request = HostRuntimeRequest(
        action="stop",
        fence=claim.fence,
        arguments=[],
        stop_plan=stop,
        run_generation=stop.run_generation,
    )
    with sessions() as session:
        parent = _required(
            session.get(Job, fenced_operation(sessions, claim).parent_job_id)
        )
        operation = _required(
            session.get(AgentOperation, fenced_operation(sessions, claim).id)
        )
        binding = derive_runtime_plan_binding(
            session,
            parent=parent,
            operation=operation,
            node_id=node_id,
            action=ContainerRuntimeAction.STOP,
            stop_plan_sha256=hashlib.sha256(canonical_message(stop)).hexdigest(),
        )
    signer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(b"m" * 32),
        clock=lambda: grant_now,
    )
    authority = HostRuntimeAuthorityService(sessions, signer, clock=lambda: grant_now)
    grant = authority.issue_grant(
        node_id=node_id,
        fence=claim.fence,
        action=ContainerRuntimeAction.STOP,
        request_sha256=hashlib.sha256(canonical_message(request)).hexdigest(),
        certificate_serial=certificate_serial,
        start_plan_sha256=binding.start_plan_sha256,
        stop_plan_sha256=binding.stop_plan_sha256,
        run_generation=binding.run_generation,
        runtime_run_id=binding.runtime_run_id,
        runtime_target_id=binding.runtime_target_id,
        runtime_installation_id=binding.runtime_installation_id,
    )
    operation_claim = grant.claims.operation
    assert isinstance(operation_claim, ExecuteContainerRuntimeRequestOperation)
    assert operation_claim.stop_plan_sha256 == binding.stop_plan_sha256
    assert operation_claim.runtime_target_id == stop.target_runtime_id
    signer.public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )
    return claim, stop, grant


@pytest.mark.parametrize("newer_intent", ["none", "all-nodes", "one-node"])
def test_every_failed_start_leaves_what_it_launched_stoppable_or_queues_its_stop(
    tmp_path: Path, newer_intent: str
) -> None:
    """A Start that ended failed may have launched ranks. Whatever intent owns the
    Sparks by then, the run is never left ``failed`` (which nothing stops): a stop
    is queued (under the intent that owns the Sparks now, never taking a new one),
    or, with no single owner, the run stays stoppable for its owner."""

    from vonk_control.models import STOPPABLE_RUN_STATES

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


def test_postgres_duplicate_stop_request_returns_same_operation(
    tmp_path: Path, postgres_engine
) -> None:
    # Both Stops carry a preview the run has moved on from: the first is
    # re-planned, never refused. The duplicate pauses at its run fence while
    # the first is accepted, withdrawn and dispatched, then returns that Stop.
    Base.metadata.drop_all(postgres_engine)
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="e" * 35 + "5"
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="e" * 35 + "6",
    )
    publisher = ConcurrentPublisher()
    service, routes = bind_route_publications(sessions, service, publisher)
    routes.publish_run(run.owner_id)
    plan = service.preview_stop(run.owner_id)
    with sessions.begin() as session:
        moved = session.get(RecipeRun, run.owner_id)
        assert moved is not None
        moved.state = "failed"
    assert service.preview_stop(run.owner_id).plan_digest != plan.plan_digest
    request_key = "e" * 35 + "7"
    role = threading.local()
    second_at_fence = threading.Event()
    release_second = threading.Event()

    def at_fence(_connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "second"
            and "FROM recipe_runs" in statement
            and "FOR UPDATE" in statement
            and not second_at_fence.is_set()
        ):
            second_at_fence.set()
            assert release_second.wait(timeout=10)

    def stop(label: str) -> str:
        role.value = label
        return service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_key,
        ).id

    event.listen(postgres_engine, "before_cursor_execute", at_fence)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        second = pool.submit(stop, "second")
        assert second_at_fence.wait(timeout=10)
        first_id = pool.submit(stop, "first").result(timeout=10)
        release_second.set()
        second_id = second.result(timeout=10)
    finally:
        release_second.set()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", at_fence)

    assert second_id == first_id
    with sessions() as session:
        assert (
            len(
                tuple(session.scalars(select(Job).where(Job.request_id == request_key)))
            )
            == 1
        )


@pytest.mark.parametrize("release_on_wait", [True, False], ids=["released", "deadline"])
def test_postgres_duplicate_stop_waiting_on_dispatch_replays(
    tmp_path: Path,
    postgres_engine,
    monkeypatch: pytest.MonkeyPatch,
    release_on_wait: bool,
) -> None:
    # Dispatch already owns the accepted parent and run. Re-taking the request
    # creation key here makes its own duplicate spend every try-lock attempt
    # before it can read that receipt. Hold dispatch until the duplicate waits
    # on its run, then let the committed receipt answer both callers.
    from vonk_control.recipe_operations import persistence

    Base.metadata.drop_all(postgres_engine)
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="e" * 35 + "b"
    )
    run = started_recipe(
        sessions, service, installation.owner_id, nodes, request_id="e" * 35 + "c"
    )
    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    routes.publish_run(run.owner_id)
    plan = service.preview_stop(run.owner_id)
    request_key = "e" * 35 + "d"
    role = threading.local()
    backend_pids: dict[str, int] = {}
    dispatch_locked = threading.Event()
    duplicate_progress = threading.Event()
    release_dispatch = threading.Event()
    acquire = persistence.acquire_admission_keys

    def held_dispatch(session, keys, *, holder=None):
        acquire(session, keys, holder=holder)
        if getattr(role, "value", None) == "first":
            backend_pids["first"] = _postgres_backend_pid(session.connection())
            dispatch_locked.set()
            assert release_dispatch.wait(timeout=10)

    def before_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "second"
            and "FROM recipe_runs" in statement
            and "FOR UPDATE" in statement
        ):
            backend_pids["second"] = _postgres_backend_pid(connection)
            duplicate_progress.set()

    def stop(label: str) -> str:
        role.value = label
        return service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_key,
        ).id

    monkeypatch.setattr(persistence, "acquire_admission_keys", held_dispatch)
    event.listen(postgres_engine, "before_cursor_execute", before_lock)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        first = pool.submit(stop, "first")
        assert dispatch_locked.wait(timeout=10)
        second = pool.submit(stop, "second")
        second.add_done_callback(lambda _future: duplicate_progress.set())
        assert duplicate_progress.wait(timeout=10)
        if second.done():
            # Surface an early refusal as its actual error, not a sync timeout.
            second.result()
            pytest.fail("duplicate Stop returned before dispatch released its run")
        _wait_for_postgres_block(
            postgres_engine,
            blocked_pid=backend_pids["second"],
            blocker_pid=backend_pids["first"],
        )
        if release_on_wait:
            release_dispatch.set()
        # At the SQL lock deadline the duplicate must still return the receipt,
        # while dispatch remains held. It neither cancels nor fails that Stop.
        second_id = second.result(timeout=10)
        release_dispatch.set()
        first_id = first.result(timeout=10)
    finally:
        release_dispatch.set()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", before_lock)

    assert second_id == first_id
    with sessions() as session:
        assert [
            job.id
            for job in session.scalars(select(Job).where(Job.request_id == request_key))
        ] == [first_id]
        assert len(
            tuple(
                session.scalars(
                    select(AgentOperation.id).where(
                        AgentOperation.parent_job_id == first_id
                    )
                )
            )
        ) == len(nodes)


def test_postgres_duplicate_stop_waiting_on_withdrawal_claim_replays(
    tmp_path: Path, postgres_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first Stop holds the run lock in its withdrawal-claim transaction
    # (route WITHDRAWN, review stage advanced, not yet committed) while the
    # duplicate arrives; the duplicate must replay, never be refused.
    Base.metadata.drop_all(postgres_engine)
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="e" * 35 + "8"
    )
    run = started_recipe(
        sessions, service, installation.owner_id, nodes, request_id="e" * 35 + "9"
    )
    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    routes.publish_run(run.owner_id)
    plan = service.preview_stop(run.owner_id)
    request_key = "e" * 35 + "a"
    role = threading.local()
    backend_pids: dict[str, int] = {}
    first_claimed = threading.Event()
    second_lock_started = threading.Event()
    release_first = threading.Event()
    withdrawal_publication = routes._withdrawal_publication

    def held_withdrawal_publication(session, withdrawal, *, after=None):
        publication = withdrawal_publication(session, withdrawal, after=after)
        if getattr(role, "value", None) == "first" and not first_claimed.is_set():
            backend_pids["first"] = _postgres_backend_pid(session.connection())
            first_claimed.set()
            assert release_first.wait(timeout=10)
        return publication

    def before_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "second"
            and "FROM recipe_runs" in statement
            and "FOR UPDATE" in statement
            and not second_lock_started.is_set()
        ):
            backend_pids["second"] = _postgres_backend_pid(connection)
            second_lock_started.set()

    def stop(label: str) -> str:
        role.value = label
        return service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_key,
        ).id

    monkeypatch.setattr(routes, "_withdrawal_publication", held_withdrawal_publication)
    event.listen(postgres_engine, "before_cursor_execute", before_lock)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        first = pool.submit(stop, "first")
        assert first_claimed.wait(timeout=10)
        second = pool.submit(stop, "second")
        assert second_lock_started.wait(timeout=10)
        _wait_for_postgres_block(
            postgres_engine,
            blocked_pid=backend_pids["second"],
            blocker_pid=backend_pids["first"],
        )
        release_first.set()
        first_id = first.result(timeout=10)
        second_id = second.result(timeout=10)
    finally:
        release_first.set()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", before_lock)

    assert second_id == first_id
    with sessions() as session:
        assert [
            job.id
            for job in session.scalars(select(Job).where(Job.request_id == request_key))
        ] == [first_id]


def test_postgres_pending_stop_reconciler_waits_for_dispatch_of_accepted_stop(
    tmp_path: Path, postgres_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reconciler ends an overdue accepted Stop holding only its Job lock.
    # While the Stop is being dispatched it must wait for that dispatch and
    # then leave the dispatched Stop alone: never fail a Stop whose children
    # are being queued, which would leave work without a live parent.
    Base.metadata.drop_all(postgres_engine)
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="e" * 35 + "b"
    )
    run = started_recipe(
        sessions, service, installation.owner_id, nodes, request_id="e" * 35 + "c"
    )
    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    routes.publish_run(run.owner_id)
    plan = service.preview_stop(run.owner_id)
    request_key = "e" * 35 + "d"
    role = threading.local()
    clocks = {
        "dispatch": NOW + timedelta(seconds=1),
        "reconciler": NOW + timedelta(seconds=SUPERSEDED_CANCELLATION_SECONDS + 1),
    }
    service._clock = lambda: clocks.get(getattr(role, "value", ""), NOW)
    backend_pids: dict[str, int] = {}
    dispatch_paused = threading.Event()
    reconciler_waits = threading.Event()
    release_dispatch = threading.Event()
    dispatch = service._dispatch_stop_after_withdrawal

    def dispatching(*args, **kwargs):
        role.value = "dispatch"
        return dispatch(*args, **kwargs)

    def sync_point(connection, _cursor, statement, _parameters, _context, _many):
        label = getattr(role, "value", None)
        if (
            label == "dispatch"
            and "FROM recipe_runs" in statement
            and "FOR UPDATE" in statement
            and not dispatch_paused.is_set()
        ):
            backend_pids["dispatch"] = _postgres_backend_pid(connection)
            dispatch_paused.set()
            assert release_dispatch.wait(timeout=10)
        elif (
            label == "reconciler"
            and "FROM jobs" in statement
            and "FOR UPDATE" in statement
            and "NOWAIT" not in statement
            and not reconciler_waits.is_set()
        ):
            backend_pids["reconciler"] = _postgres_backend_pid(connection)
            reconciler_waits.set()

    def stop() -> str:
        role.value = "first"
        return service.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=request_key,
        ).id

    def reconcile() -> bool:
        role.value = "reconciler"
        return service.reconcile_pending_service_stops()

    monkeypatch.setattr(service, "_dispatch_stop_after_withdrawal", dispatching)
    event.listen(postgres_engine, "before_cursor_execute", sync_point)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        stopped = pool.submit(stop)
        assert dispatch_paused.wait(timeout=10)
        reconciled = pool.submit(reconcile)
        assert reconciler_waits.wait(timeout=10)
        _wait_for_postgres_block(
            postgres_engine,
            blocked_pid=backend_pids["reconciler"],
            blocker_pid=backend_pids["dispatch"],
        )
        release_dispatch.set()
        stop_id = stopped.result(timeout=10)
        reconciled.result(timeout=10)
    finally:
        release_dispatch.set()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", sync_point)

    with sessions() as session:
        job = session.get(Job, stop_id)
        assert job is not None
        assert job.state == LifecycleState.RUNNING.value
        review = RecipeStopParent.model_validate_json(
            canonical_message(job.payload)
        ).service_stop_review
        assert review is not None and review.stage == "dispatched"
        assert session.scalar(
            select(AgentOperation.id).where(AgentOperation.parent_job_id == stop_id)
        )


def test_postgres_duplicate_uninstall_rechecks_replay_after_installation_lock(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="f" * 35 + "2"
    )
    request_key = "f" * 35 + "3"
    available_before = queue.available
    role = threading.local()
    backend_pids: dict[str, int] = {}
    first_locked = threading.Event()
    second_lock_started = threading.Event()
    release_first = threading.Event()

    def before_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "second"
            and "FROM recipe_installations" in statement
            and "FOR UPDATE" in statement
        ):
            backend_pids["second"] = _postgres_backend_pid(connection)
            second_lock_started.set()

    def after_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "first"
            and "FROM recipe_installations" in statement
            and "FOR UPDATE" in statement
        ):
            backend_pids["first"] = _postgres_backend_pid(connection)
            first_locked.set()
            assert release_first.wait(timeout=10)

    def uninstall(label: str):
        role.value = label
        return service.uninstall(
            installation.owner_id,
            actor="admin",
            request_id=request_key,
        )

    event.listen(postgres_engine, "before_cursor_execute", before_lock)
    event.listen(postgres_engine, "after_cursor_execute", after_lock)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        first = pool.submit(uninstall, "first")
        assert first_locked.wait(timeout=10)
        second = pool.submit(uninstall, "second")
        assert second_lock_started.wait(timeout=10)
        _wait_for_postgres_block(
            postgres_engine,
            blocked_pid=backend_pids["second"],
            blocker_pid=backend_pids["first"],
        )
        release_first.set()
        first_view = first.result(timeout=10)
        second_view = second.result(timeout=10)
    finally:
        release_first.set()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", before_lock)
        event.remove(postgres_engine, "after_cursor_execute", after_lock)

    assert second_view == first_view
    assert queue.available == available_before + 1
    with sessions() as session:
        parents = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.uninstall"))
        )
        children = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == first_view.id
                )
            )
        )
    assert [parent.id for parent in parents] == [first_view.id]
    assert {child.node_id for child in children} == set(nodes)


def test_postgres_start_waiting_on_accepted_uninstall_is_rejected(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="f" * 35 + "4"
    )
    run_plan = service.preview_run(installation.owner_id, "must-not-start")
    available_before = queue.available
    role = threading.local()
    uninstall_locked = threading.Event()
    start_lock_started = threading.Event()
    release_uninstall = threading.Event()

    def before_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "start"
            and "FROM recipe_installations" in statement
            and "FOR UPDATE" in statement
        ):
            assert "NOWAIT" in statement.upper()
            start_lock_started.set()

    def after_lock(connection, _cursor, statement, _parameters, _context, _many):
        if (
            getattr(role, "value", None) == "uninstall"
            and "FROM recipe_installations" in statement
            and "FOR UPDATE" in statement
        ):
            uninstall_locked.set()
            # Outlives the observer's bound: only a bounded start returns.
            assert release_uninstall.wait(timeout=60)

    def uninstall():
        role.value = "uninstall"
        return service.uninstall(
            installation.owner_id,
            actor="admin",
            request_id="f" * 35 + "5",
        )

    def start():
        role.value = "start"
        try:
            return service.start(
                run_plan,
                plan_digest=run_plan.plan_digest,
                actor="admin",
                request_id="f" * 35 + "6",
            )
        except (RecipeOperationConflict, RunAdmissionBusy) as error:
            return error

    event.listen(postgres_engine, "before_cursor_execute", before_lock)
    event.listen(postgres_engine, "after_cursor_execute", after_lock)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        uninstall_future = pool.submit(uninstall)
        assert uninstall_locked.wait(timeout=10)
        start_future = pool.submit(start)
        assert start_lock_started.wait(timeout=10)
        completed, _ = wait((start_future,), timeout=30)
        returned_while_uninstall_held = start_future in completed
        start_result_while_held = (
            start_future.result() if returned_while_uninstall_held else None
        )
        release_uninstall.set()
        uninstall_view = uninstall_future.result(timeout=10)
        start_result = (
            start_result_while_held
            if returned_while_uninstall_held
            else start_future.result(timeout=10)
        )
    finally:
        release_uninstall.set()
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", before_lock)
        event.remove(postgres_engine, "after_cursor_execute", after_lock)

    assert returned_while_uninstall_held, (
        "start waited on the accepted uninstall instead of returning a bounded "
        f"admission refusal: {start_result!r}"
    )
    assert isinstance(start_result, RunAdmissionBusy), (
        "start did not report retryable row-lock contention while uninstall "
        f"was still held: {start_result!r}"
    )

    # The busy refusal has no durable request owner, so the exact request can
    # be retried after the uninstall commits. At that point the committed
    # uninstall is authoritative and refuses the run for its actual reason.
    with pytest.raises(Exception) as _ending:
        service.start(
            run_plan,
            plan_digest=run_plan.plan_digest,
            actor="admin",
            request_id="f" * 35 + "6",
        )
    assert queue.available == available_before + 1
    with sessions() as session:
        start_jobs = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.start"))
        )
        runs = tuple(session.scalars(select(RecipeRun)))
        uninstall_children = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == uninstall_view.id
                )
            )
        )
    assert start_jobs == ()
    assert runs == ()
    assert {child.node_id for child in uninstall_children} == set(nodes)


def test_postgres_start_refuses_without_waiting_on_a_busy_agent_node(
    tmp_path: Path, postgres_engine
) -> None:
    """A node-row conflict returns a domain refusal before releasing its holder."""
    Base.metadata.drop_all(postgres_engine)
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="e" * 36
    )
    plan = service.preview_run(installation.owner_id, "busy-node")
    assert plan.allowed
    available_before = queue.available
    role = threading.local()
    start_node_lock_requested = threading.Event()

    def observe_start_node_lock(
        _connection, _cursor, statement, _parameters, _context, _many
    ):
        if (
            getattr(role, "value", None) == "start"
            and "FROM agent_nodes" in statement
            and "FOR NO KEY UPDATE" in statement
        ):
            start_node_lock_requested.set()

    def start():
        role.value = "start"
        return service.start(
            plan,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="e" * 35 + "1",
        )

    event.listen(postgres_engine, "before_cursor_execute", observe_start_node_lock)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with sessions() as holder:
            holder.begin()
            locked_node = holder.scalar(
                select(AgentNode).where(AgentNode.node_id == nodes[0]).with_for_update()
            )
            assert locked_node is not None
            future = pool.submit(start)
            lock_requested = start_node_lock_requested.wait(timeout=10)
            # The holder keeps the row for longer than every admission bound
            # (lock timeout, bounded retries), however slow the runner: a start
            # that waits for the release cannot answer inside this window.
            completed, _ = wait((future,), timeout=10)
            returned_while_held = future in completed
            held_error = future.exception() if returned_while_held else None
            holder.commit()

        try:
            start_result = future.result(timeout=10)
            start_error: Exception | None = None
        except (RecipeOperationConflict, RunAdmissionBusy) as error:
            start_result = None
            start_error = error
    finally:
        pool.shutdown(wait=True)
        event.remove(postgres_engine, "before_cursor_execute", observe_start_node_lock)

    assert lock_requested, "start never attempted to lock the target AgentNode"
    assert returned_while_held, (
        "lifecycle start waited for a busy AgentNode row; after holder release "
        f"it returned {start_result!r} with error={start_error!r} "
        f"(while held: {held_error!r})"
    )
    assert isinstance(held_error, RunAdmissionBusy), (
        f"start did not return the current refusal while the row was held: "
        f"error={held_error!r} result_after_release={start_result!r} "
        f"error_after_release={start_error!r}"
    )
    assert queue.available == available_before
    with sessions() as session:
        start_jobs = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.start"))
        )
        runs = tuple(session.scalars(select(RecipeRun)))
    assert start_jobs == ()
    assert runs == ()

    request_id = "e" * 35 + "1"
    retried = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=request_id,
    )
    assert queue.available == available_before + 1
    replay = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=request_id,
    )
    assert replay.id == retried.id
    assert queue.available == available_before + 1
    with sessions() as session:
        assert (
            len(tuple(session.scalars(select(Job).where(Job.kind == "recipe.start"))))
            == 1
        )
        assert len(tuple(session.scalars(select(RecipeRun)))) == 1


def test_postgres_start_bounds_uncommitted_job_request_unique_wait(
    tmp_path: Path, postgres_engine
) -> None:
    """An implicit unique-index wait rolls back and preserves exact-key retry."""
    Base.metadata.drop_all(postgres_engine)
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="d" * 36
    )
    plan = service.preview_run(installation.owner_id, "busy-request")
    assert plan.allowed
    request_id = "e" * 35 + "2"
    available_before = queue.available
    created_at = NOW
    holder = sessions()
    holder.begin()
    holder.add(
        Job(
            id="f" * 36,
            request_id=request_id,
            kind="recipe.start",
            state="running",
            actor="admin",
            authority_revision="a" * 64,
            targets=list(nodes),
            payload_digest="b" * 64,
            payload={
                "schema_version": 1,
                "owner_kind": "run",
                "owner_id": "c" * 36,
                "plan_digest": plan.plan_digest,
            },
            created_at=created_at,
            updated_at=created_at,
        )
    )
    holder.flush()

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        service.start,
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=request_id,
    )
    try:
        completed, _ = wait((future,), timeout=30)
        returned_while_unique_row_uncommitted = future in completed
        busy_error = (
            future.exception() if returned_while_unique_row_uncommitted else None
        )
    finally:
        holder.rollback()
        holder.close()
        if not future.done():
            future.result(timeout=10)
        pool.shutdown(wait=True)

    assert returned_while_unique_row_uncommitted, (
        "start waited on the jobs.request_id unique index; the admission lock "
        "budget must bound implicit unique waits"
    )
    assert isinstance(busy_error, RunAdmissionBusy), (
        f"uncommitted request collision did not become a retryable admission "
        f"refusal: {busy_error!r}"
    )
    assert queue.available == available_before
    with sessions() as session:
        assert tuple(session.scalars(select(RecipeRun))) == ()
        assert (
            tuple(session.scalars(select(Job).where(Job.kind == "recipe.start"))) == ()
        )
        assert (
            tuple(
                session.scalars(
                    select(ResourceReservation).where(
                        ResourceReservation.owner_kind == "run"
                    )
                )
            )
            == ()
        )

    retried = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=request_id,
    )
    replay = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=request_id,
    )
    assert replay.id == retried.id
    assert queue.available == available_before + 1
    with sessions() as session:
        assert len(tuple(session.scalars(select(RecipeRun)))) == 1
        assert (
            len(tuple(session.scalars(select(Job).where(Job.kind == "recipe.start"))))
            == 1
        )
