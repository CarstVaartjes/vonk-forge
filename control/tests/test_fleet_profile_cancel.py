"""Profile cancellation crosses the real API and Run/Switch owners."""

from __future__ import annotations

import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentResult
from vonk_control.agent_jobs import AgentJobService
from vonk_control.auth import Actor, TokenCodec
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.fleet_profile_contract import (
    FleetProfileChildOperation,
    FleetProfileInput,
)
from vonk_control.fleet_profiles import (
    RunSwitchFleetProfileAdapter,
    build_production_fleet_profile_service,
)
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    CatalogDocumentRevision,
    FleetProfileApplication,
    Job,
    RecipeRun,
    ResourceReservation,
    RunNode,
    User,
)
from vonk_control.operation_api import durable_operation_services
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.run_admission import RunAdmissionService
from vonk_control.run_switch_operations import RunSwitchOperationService

from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, claim_agent
from .test_fleet_profile_api import _client, _headers
from .test_fleet_profiles import _uuid
from .test_recipe_operations import (
    NOW,
    installed_recipe,
    setup_services,
    started_recipe,
)
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)


class _IdleRoutes:
    """The cancellation worker has no route publication dependency."""

    def publish_run(self, run_id: str) -> object:
        del run_id
        return None

    def maintain(self, *, renew_before_seconds: int = 10) -> bool:
        del renew_before_seconds
        return False


def _profile_worker_process_dies_with_pending_cancel(
    database_url: str, now_text: str
) -> None:
    """Rebuild the production owners, observe once, then die like a worker."""
    from sqlalchemy import create_engine

    engine = create_engine(
        database_url, connect_args={"options": "-c lock_timeout=3000"}
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime.fromisoformat(now_text)
    clock = lambda: now
    agent_jobs = AgentJobService(sessions, clock=clock)
    lifecycle = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions, disk_floor_bytes=10),
        run_admission=RunAdmissionService(sessions, memory_floor_bytes=50),
        agent_jobs=agent_jobs,
        clock=clock,
    )
    agent_jobs.set_result_consumer(lifecycle.consume_agent_result)
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    profiles = build_production_fleet_profile_service(
        sessions, clock=clock, run_switch_operations=run_switch
    )
    worker = RecipeOperationWorker(
        sessions,
        _IdleRoutes(),
        clock=clock,
        fleet_profiles=profiles,
        run_switches=run_switch,
    )
    worker.tick()
    os._exit(23)


def _resume_profile_cancel_process(
    database_url: str, now_text: str, application_id: str
) -> None:
    """A fresh real worker settles the exact child after its receipt arrives."""
    from sqlalchemy import create_engine

    engine = create_engine(
        database_url, connect_args={"options": "-c lock_timeout=3000"}
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime.fromisoformat(now_text)
    clock = lambda: now
    agent_jobs = AgentJobService(sessions, clock=clock)
    lifecycle = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions, disk_floor_bytes=10),
        run_admission=RunAdmissionService(sessions, memory_floor_bytes=50),
        agent_jobs=agent_jobs,
        clock=clock,
    )
    agent_jobs.set_result_consumer(lifecycle.consume_agent_result)
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    profiles = build_production_fleet_profile_service(
        sessions, clock=clock, run_switch_operations=run_switch
    )
    worker = RecipeOperationWorker(
        sessions,
        _IdleRoutes(),
        clock=clock,
        fleet_profiles=profiles,
        run_switches=run_switch,
    )
    for _ in range(6):
        worker.tick()
        if profiles.application(application_id).state == "cancelled":
            return
    raise AssertionError("fresh worker did not settle the profile cancellation")


def _agent_result(claim, *, state: str, result: dict[str, object]) -> AgentResult:
    return AgentResult.model_validate(
        {
            "schema_version": 1,
            "job_id": claim.job_id,
            "operation_id": claim.operation_id,
            "attempt": claim.attempt,
            "fence": claim.fence,
            "node_id": claim.node_id,
            "deadline": claim.deadline,
            "state": state,
            "result": result,
        }
    )


def _two_target_stop_case(tmp_path, *, engine):
    now = [NOW]
    clock = lambda: now[0]
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=engine
    )
    lifecycle._clock = clock
    installed = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=_uuid(986)
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installed.owner_id,
        nodes,
        request_id=_uuid(987),
        alias="cancel-targets",
    )
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    service = build_production_fleet_profile_service(
        sessions, clock=clock, run_switch_operations=run_switch
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {"name": "Stop all workloads", "assignments": []}
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed, preview.reasons
    assert preview.summary.stops == 1
    application = service.load(
        profile.number,
        request_key=_uuid(988),
        actor="admin",
        expected_plan_digest=preview.plan_digest,
    )
    return (
        sessions,
        lifecycle,
        run_switch,
        service,
        profile,
        application,
        run,
        nodes,
        now,
    )


def _start_profile_stop_child(sessions, run_switch, service, application):
    for _ in range(4):
        service.tick()
        run_switch.tick()
        active = service.application(application.id).progress.switch_adapter
        if active is not None and active.active_operation_id is not None:
            with sessions() as session:
                stop_job = session.scalar(select(Job).where(Job.kind == "recipe.stop"))
            if stop_job is not None:
                return active.active_operation_id, stop_job.id
    raise AssertionError("profile did not issue its exact target stop child")


def _agent_service_and_target_claim(
    sessions, lifecycle, node_id, nodes, *, clock, jobs: AgentJobService | None = None
):
    capabilities = ["agent.runtime.rust.v1", "recipe.stop", "recipe.operations.v1"]
    if jobs is None:
        with sessions.begin() as session:
            for node in session.scalars(
                select(AgentNode).where(AgentNode.node_id.in_(nodes))
            ):
                node.capabilities = sorted(set(node.capabilities) | set(capabilities))
        jobs = AgentJobService(
            sessions, clock=clock, result_consumer=lifecycle.consume_agent_result
        )
    index = nodes.index(node_id)
    with sessions() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        identity = {**PACKAGED_RUNTIME_IDENTITY, "architecture": node.architecture}
        if node.observation_receipt_public_key is not None:
            identity["observation_receipt_public_key"] = (
                node.observation_receipt_public_key
            )
    claim = claim_agent(
        jobs,
        node_id,
        f"serial-{index}",
        300,
        capabilities=capabilities,
        runtime_identity=identity,
    )
    return jobs, claim


def test_profile_cancel_after_one_of_two_stop_targets_preserves_exact_partial_effects(
    tmp_path, postgres_engine
) -> None:
    (
        sessions,
        lifecycle,
        run_switch,
        service,
        profile,
        application,
        run,
        nodes,
        now,
    ) = _two_target_stop_case(tmp_path, engine=postgres_engine)
    child_id, stop_job_id = _start_profile_stop_child(
        sessions, run_switch, service, application
    )
    agent_jobs, first_claim = _agent_service_and_target_claim(
        sessions, lifecycle, nodes[0], nodes, clock=lambda: now[0]
    )
    assert first_claim is not None and first_claim.job_id == stop_job_id
    agent_jobs.record_result(
        _agent_result(first_claim, state="succeeded", result={"stopped": True})
    )
    _, second_claim = _agent_service_and_target_claim(
        sessions,
        lifecycle,
        nodes[1],
        nodes,
        clock=lambda: now[0],
        jobs=agent_jobs,
    )
    assert second_claim is not None and second_claim.job_id == stop_job_id

    with sessions() as session:
        first_node = session.scalar(
            select(RunNode).where(
                RunNode.run_id == run.owner_id, RunNode.node_id == nodes[0]
            )
        )
        second_node = session.scalar(
            select(RunNode).where(
                RunNode.run_id == run.owner_id, RunNode.node_id == nodes[1]
            )
        )
        assert first_node is not None and first_node.state == "stopped"
        assert second_node is not None and second_node.state != "stopped"
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run.owner_id,
                )
            )
        )
        assert claims and all(claim.state == "active" for claim in claims)

    cancel_key = _uuid(989)
    pending = service.cancel(
        application.id,
        profile_number=profile.number,
        request_key=cancel_key,
        actor="admin",
    )
    assert pending.cancellation is not None
    assert pending.cancellation.state == "cancelling"
    assert pending.cancellation.dependency == child_id
    assert any(
        effect.operation_id == child_id
        for effect in pending.cancellation.pending_effects
    )
    with sessions() as session:
        second_operation = session.get(AgentOperation, second_claim.operation_id)
        assert second_operation is not None and second_operation.state == "running"
        assert second_operation.parent_job_id == stop_job_id
    directive = agent_jobs.heartbeat(second_claim, None, 30)
    assert directive.cancel_requested is True
    # The native stop executor emits this result only after the exact host
    # STOP and runtime completion receipt have both succeeded.
    agent_jobs.record_result(
        _agent_result(
            second_claim,
            state="cancelled",
            result={
                "error_code": "operation_cancelled",
                "reason": "profile cancellation stopped the outstanding target effect",
            },
        )
    )
    run_switch.tick()
    service.tick()

    terminal = service.application(application.id)
    assert terminal.state == "cancelled"
    assert terminal.cancellation is not None
    assert terminal.cancellation.state == "cancelled"
    assert terminal.cancellation.pending_effects == []
    assert any(
        effect.operation_id == child_id and effect.outcome == "cancelled"
        for effect in terminal.cancellation.cancelled_effects
    )
    assert run_switch.get(child_id).state == "cancelled"
    with sessions() as session:
        stop_jobs = tuple(session.scalars(select(Job).where(Job.kind == "recipe.stop")))
        first_operation = session.get(AgentOperation, first_claim.operation_id)
        second_operation = session.get(AgentOperation, second_claim.operation_id)
        first_node = session.scalar(
            select(RunNode).where(
                RunNode.run_id == run.owner_id, RunNode.node_id == nodes[0]
            )
        )
        second_node = session.scalar(
            select(RunNode).where(
                RunNode.run_id == run.owner_id, RunNode.node_id == nodes[1]
            )
        )
    assert [item.id for item in stop_jobs] == [stop_job_id]
    assert first_operation is not None and first_operation.state == "succeeded"
    assert second_operation is not None and second_operation.state == "cancelled"
    assert first_node is not None and first_node.state == "stopped"
    assert second_node is not None and second_node.state == "stopped"


def test_profile_cancel_pending_child_survives_os_worker_death_and_restarts(
    tmp_path, postgres_engine
) -> None:
    (
        sessions,
        lifecycle,
        run_switch,
        service,
        profile,
        application,
        run,
        nodes,
        now,
    ) = _two_target_stop_case(tmp_path, engine=postgres_engine)
    child_id, stop_job_id = _start_profile_stop_child(
        sessions, run_switch, service, application
    )
    agent_jobs, claim = _agent_service_and_target_claim(
        sessions, lifecycle, nodes[0], nodes, clock=lambda: now[0]
    )
    assert claim is not None and claim.job_id == stop_job_id
    pending = service.cancel(
        application.id,
        profile_number=profile.number,
        request_key=_uuid(990),
        actor="admin",
    )
    assert pending.cancellation is not None
    assert pending.cancellation.state == "cancelling"
    assert pending.cancellation.dependency == child_id

    context = multiprocessing.get_context("spawn")
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    dead_worker = context.Process(
        target=_profile_worker_process_dies_with_pending_cancel,
        args=(database_url, now[0].isoformat()),
    )
    dead_worker.start()
    dead_worker.join(timeout=20)
    if dead_worker.is_alive():
        dead_worker.terminate()
        dead_worker.join(timeout=5)
    assert dead_worker.exitcode == 23
    with sessions() as session:
        durable = session.get(FleetProfileApplication, application.id)
        parent_job = session.get(Job, child_id)
        stop_job = session.get(Job, stop_job_id)
        operation = session.get(AgentOperation, claim.operation_id)
        assert durable is not None
        assert durable.state == "running"
        cancellation_progress = durable.progress.get("cancellation")
        assert isinstance(cancellation_progress, dict)
        assert cancellation_progress.get("state") == "cancelling"
        assert parent_job is not None
        assert parent_job.result is not None
        cancellation_result = parent_job.result.get("cancellation")
        assert isinstance(cancellation_result, dict)
        assert cancellation_result.get("request_key") == _uuid(990)
        assert stop_job is not None and stop_job.state == "running"
        assert operation is not None and operation.state == "running"
        retained = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run.owner_id,
                )
            )
        )
        assert retained and all(item.state == "active" for item in retained)

    # The exact stop owner supplies the result after the first worker dies.
    assert agent_jobs.heartbeat(claim, None, 30).cancel_requested is True
    agent_jobs.record_result(
        _agent_result(
            claim,
            state="cancelled",
            result={
                "error_code": "operation_cancelled",
                "reason": "profile cancellation reconciled after worker restart",
            },
        )
    )
    now[0] += timedelta(seconds=61)
    restarted = context.Process(
        target=_resume_profile_cancel_process,
        args=(database_url, now[0].isoformat(), application.id),
    )
    restarted.start()
    restarted.join(timeout=20)
    if restarted.is_alive():
        restarted.terminate()
        restarted.join(timeout=5)
    assert restarted.exitcode == 0
    terminal = service.application(application.id)
    assert terminal.state == "cancelled"
    assert terminal.cancellation is not None
    assert terminal.cancellation.state == "cancelled"
    assert terminal.cancellation.pending_effects == []
    assert run_switch.get(child_id).state == "cancelled"
    with sessions() as session:
        stop_jobs = tuple(session.scalars(select(Job).where(Job.kind == "recipe.stop")))
        run_row = session.get(RecipeRun, run.owner_id)
    assert [item.id for item in stop_jobs] == [stop_job_id]
    assert run_row is not None and run_row.state in {"lost", "stopped"}


def test_newer_profile_load_replaces_pending_cancellation_without_losing_child_owner(
    tmp_path, postgres_engine
) -> None:
    (
        sessions,
        lifecycle,
        run_switch,
        service,
        profile,
        application,
        _run,
        nodes,
        now,
    ) = _two_target_stop_case(tmp_path, engine=postgres_engine)
    child_id, stop_job_id = _start_profile_stop_child(
        sessions, run_switch, service, application
    )
    agent_jobs, first_claim = _agent_service_and_target_claim(
        sessions, lifecycle, nodes[0], nodes, clock=lambda: now[0]
    )
    assert first_claim is not None
    agent_jobs.record_result(
        _agent_result(first_claim, state="succeeded", result={"stopped": True})
    )
    _, second_claim = _agent_service_and_target_claim(
        sessions,
        lifecycle,
        nodes[1],
        nodes,
        clock=lambda: now[0],
        jobs=agent_jobs,
    )
    assert second_claim is not None
    pending = service.cancel(
        application.id,
        profile_number=profile.number,
        request_key=_uuid(991),
        actor="admin",
    )
    assert pending.cancellation is not None
    assert pending.cancellation.state == "cancelling"
    assert pending.cancellation.dependency == child_id

    fresh = service.preview(profile.id)
    assert fresh.allowed, fresh.reasons
    replacement = service.load(
        profile.number,
        request_key=_uuid(992),
        actor="admin",
        expected_plan_digest=fresh.plan_digest,
    )

    with sessions() as session:
        old = session.get(FleetProfileApplication, application.id)
        new = session.get(FleetProfileApplication, replacement.id)
        node_rows = tuple(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(nodes))
                .order_by(AgentNode.node_id)
            )
        )
        old_child = session.get(Job, child_id)
        stop_job = session.get(Job, stop_job_id)
        issued = session.get(AgentOperation, second_claim.operation_id)
    assert old is not None and old.state == "cancelled"
    assert new is not None and new.state == "queued"
    assert old_child is not None and old_child.id == child_id
    assert stop_job is not None and stop_job.state == "running"
    assert issued is not None and issued.state == "running"
    assert issued.parent_job_id == stop_job_id
    new_ordinal = new.progress.get("workload_intent_ordinal")
    old_ordinal = old.progress.get("workload_intent_ordinal")
    assert isinstance(new_ordinal, int) and isinstance(old_ordinal, int)
    assert node_rows and {node.workload_intent_ordinal for node in node_rows} == {
        new_ordinal
    }
    assert new_ordinal > old_ordinal
    assert run_switch.tick()
    assert run_switch.get(child_id).state == "cancelled"
    assert service.application(application.id).state == "cancelled"

    # A late exact stop receipt settles its issued cleanup owner only; it
    # cannot reopen the superseded profile or change the replacement intent.
    directive = agent_jobs.heartbeat(second_claim, None, 30)
    assert directive.cancel_requested is True
    agent_jobs.record_result(
        _agent_result(
            second_claim,
            state="cancelled",
            result={
                "error_code": "operation_cancelled",
                "reason": "controller cancellation confirmed after exact workload stop",
            },
        )
    )
    with sessions() as session:
        settled = session.get(AgentOperation, second_claim.operation_id)
        old_after_receipt = session.get(FleetProfileApplication, application.id)
        new_after_receipt = session.get(FleetProfileApplication, replacement.id)
        nodes_after_receipt = tuple(
            session.scalars(select(AgentNode).where(AgentNode.node_id.in_(nodes)))
        )
    assert settled is not None and settled.state == "cancelled"
    assert old_after_receipt is not None and old_after_receipt.state == "cancelled"
    assert new_after_receipt is not None and new_after_receipt.state == "queued"
    assert {node.workload_intent_ordinal for node in nodes_after_receipt} == {
        new_ordinal
    }


def _application(
    tmp_path,
    *,
    engine=None,
):
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, engine=engine
    )
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    service = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=run_switch
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Cancellation boundary",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "cancel-boundary",
                    }
                ],
            }
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed is True
    application = service.load(
        profile.number,
        request_key=_uuid(980),
        actor="admin",
        expected_plan_digest=preview.plan_digest,
    )
    adapter = service._switch_adapter
    assert isinstance(adapter, RunSwitchFleetProfileAdapter)
    return sessions, run_switch, service, profile, application, adapter, nodes


def test_profile_cancel_api_is_exact_authorized_and_stops_before_dispatch(
    tmp_path,
) -> None:
    sessions, _run_switch, service, profile, application, _adapter, _nodes = (
        _application(tmp_path)
    )
    api, tokens = _client(sessions, profiles=service)
    path = f"/api/profile/applications/{application.id}/cancel"
    key = _uuid(981)
    body = {"profile_number": profile.number, "request_key": key}

    assert api.post(path, json=body).status_code == 401
    assert api.post(path, json=body, headers=_headers(tokens)).status_code == 403
    wrong_profile = api.post(
        path,
        json={**body, "profile_number": profile.number + 1},
        headers=_headers(tokens, "administrator"),
    )
    assert wrong_profile.status_code == 404
    route = api.get("/openapi.json").json()["paths"][
        path.replace(application.id, "{application_id}")
    ]["post"]
    assert route["operationId"] == "cancelProfileApplication"
    assert "requestBody" in route

    accepted = api.post(path, json=body, headers=_headers(tokens, "administrator"))
    assert accepted.status_code == 202, accepted.text
    receipt = accepted.json()
    assert receipt["state"] == "running"
    assert receipt["cancellation"]["request_key"] == key
    assert receipt["cancellation"]["state"] == "cancelling"
    assert len(receipt["cancellation"]["cancelled_effects"]) == 1
    lookup_path = f"/api/profile/applications/{application.id}/cancellations/{key}"
    lookup = api.get(lookup_path, headers=_headers(tokens, "administrator"))
    assert lookup.status_code == 200, lookup.text
    assert lookup.json()["cancellation"]["request_key"] == key
    assert lookup.json()["cancellation"]["actor"] == "administrator"
    lookup_openapi = api.get("/openapi.json").json()["paths"][
        "/api/profile/applications/{application_id}/cancellations/{request_key}"
    ]["get"]
    assert lookup_openapi["operationId"] == "getProfileApplicationCancellation"
    wrong_request_lookup = api.get(
        f"/api/profile/applications/{application.id}/cancellations/{_uuid(982)}",
        headers=_headers(tokens, "administrator"),
    )
    assert wrong_request_lookup.status_code == 404
    with sessions() as session:
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "fleet-profile",
                    ResourceReservation.owner_id == application.id,
                )
            )
        )
    assert claims
    assert all(claim.state in {"active", "promised"} for claim in claims)

    replay = api.post(path, json=body, headers=_headers(tokens, "administrator"))
    assert replay.status_code == 202
    assert replay.json()["cancellation"]["request_key"] == key
    conflict = api.post(
        path,
        json={"profile_number": profile.number, "request_key": _uuid(982)},
        headers=_headers(tokens, "administrator"),
    )
    assert conflict.status_code == 409

    # Request identity includes the current actor. Another administrator
    # cannot replay the accepted key on this application.
    with sessions.begin() as session:
        session.add(User(subject="second-admin", role="administrator"))
    other_admin = tokens.issue(
        Actor("second-admin", "administrator"), ttl_seconds=100, now=0
    )
    other_actor = api.post(
        path,
        json=body,
        headers={"Authorization": f"Bearer {other_admin}"},
    )
    assert other_actor.status_code == 409
    other_owner_lookup = api.get(
        lookup_path,
        headers={"Authorization": f"Bearer {other_admin}"},
    )
    assert other_owner_lookup.status_code == 404

    # The receipt also checks the current database role, so an old signed
    # administrator token cannot survive a role downgrade.
    with sessions.begin() as session:
        administrator = session.scalar(
            select(User).where(User.subject == "administrator")
        )
        assert administrator is not None
        administrator.role = "operator"
    demoted_lookup = api.get(lookup_path, headers=_headers(tokens, "administrator"))
    assert demoted_lookup.status_code == 403

    # Authority is checked even for a byte-for-byte duplicate. A request
    # already accepted still remains a durable worker instruction.
    with sessions.begin() as session:
        administrator = session.scalar(
            select(User).where(User.subject == "administrator")
        )
        assert administrator is not None
        administrator.role = "administrator"
        administrator.disabled_at = datetime.now(UTC)
    revoked_replay = api.post(
        path, json=body, headers=_headers(tokens, "administrator")
    )
    assert revoked_replay.status_code == 403
    revoked_lookup = api.get(lookup_path, headers=_headers(tokens, "administrator"))
    assert revoked_lookup.status_code == 403

    # The durable request prevents the worker from making its first child.
    assert service.tick() is True
    terminal = service.application(application.id)
    assert terminal.state == "cancelled"
    assert terminal.cancellation is not None
    assert terminal.cancellation.state == "cancelled"
    assert terminal.cancellation.pending_effects == []
    with sessions() as session:
        assert (
            session.scalar(select(Job.id).where(Job.kind == "recipe.run-switch.v2"))
            is None
        )
        stored = session.get(FleetProfileApplication, application.id)
        assert stored is not None
        assert stored.state == "cancelled"
        released_claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "fleet-profile",
                    ResourceReservation.owner_id == application.id,
                )
            )
        )
        assert released_claims
        assert all(claim.state == "released" for claim in released_claims)


def test_pending_profile_cancellation_is_visible_and_filterable_in_activity(
    tmp_path,
) -> None:
    sessions, _run_switch, service, profile, application, _adapter, _nodes = (
        _application(tmp_path)
    )
    operations = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=service._clock,
        cursors=TokenCodec(b"a" * 32).cursor_codec(),
        operation_providers=(service.operation_provider(),),
    )
    api, tokens = _client(sessions, profiles=service, operations=operations)
    cancel_key = _uuid(997)
    accepted = api.post(
        f"/api/profile/applications/{application.id}/cancel",
        json={"profile_number": profile.number, "request_key": cancel_key},
        headers=_headers(tokens, "administrator"),
    )
    assert accepted.status_code == 202, accepted.text

    detail = api.get(
        f"/api/operations/{application.id}",
        headers=_headers(tokens, "administrator"),
    )
    cancelling = api.get(
        "/api/operations",
        params={"state": "cancelling", "request_id": application.request_key},
        headers=_headers(tokens, "administrator"),
    )
    running = api.get(
        "/api/operations",
        params={"state": "running", "request_id": application.request_key},
        headers=_headers(tokens, "administrator"),
    )

    assert detail.status_code == 200, detail.text
    assert detail.json()["state"] == "cancelling"
    cancellation = detail.json()["cancellation"]
    assert cancellation["request_key"] == cancel_key
    assert cancellation["actor"] == "administrator"
    assert cancellation["cause"] == "operator"
    assert cancellation["pending_effects"] == []
    assert cancellation["cancelled_effects"][0]["outcome"] == "not-issued"
    # Activity's owner request id remains the original profile-load key;
    # cancellation has its own exact request identity in the nested receipt.
    assert detail.json()["owner"]["request_id"] == application.request_key
    assert detail.json()["status_reason"]
    assert cancelling.status_code == 200, cancelling.text
    assert cancelling.json()["total"] == 1
    assert cancelling.json()["operations"][0]["id"] == application.id
    assert (
        cancelling.json()["operations"][0]["cancellation"]["request_key"] == cancel_key
    )
    assert running.status_code == 200, running.text
    assert running.json()["total"] == 0

    assert service.tick() is True
    terminal = api.get(
        "/api/operations",
        params={"state": "cancelled", "request_id": application.request_key},
        headers=_headers(tokens, "administrator"),
    )
    assert terminal.status_code == 200, terminal.text
    assert terminal.json()["total"] == 1
    assert terminal.json()["operations"][0]["state"] == "cancelled"
    assert terminal.json()["operations"][0]["cancellation"]["state"] == "cancelled"


def test_profile_activity_cancellation_filter_compiles_on_postgres(
    tmp_path, postgres_engine
) -> None:
    """The displayed cancellation state and SQL filter agree on PostgreSQL."""

    sessions, _run_switch, service, profile, application, _adapter, _nodes = (
        _application(tmp_path, engine=postgres_engine)
    )
    operations = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=service._clock,
        cursors=TokenCodec(b"c" * 32).cursor_codec(),
        operation_providers=(service.operation_provider(),),
    )
    api, tokens = _client(sessions, profiles=service, operations=operations)
    accepted = api.post(
        f"/api/profile/applications/{application.id}/cancel",
        json={"profile_number": profile.number, "request_key": _uuid(999)},
        headers=_headers(tokens, "administrator"),
    )
    assert accepted.status_code == 202, accepted.text

    cancelling = api.get(
        "/api/operations",
        params={"state": "cancelling", "request_id": application.request_key},
        headers=_headers(tokens, "administrator"),
    )
    running = api.get(
        "/api/operations",
        params={"state": "running", "request_id": application.request_key},
        headers=_headers(tokens, "administrator"),
    )

    assert cancelling.status_code == 200, cancelling.text
    assert cancelling.json()["total"] == 1
    assert cancelling.json()["operations"][0]["state"] == "cancelling"
    assert running.status_code == 200, running.text
    assert running.json()["total"] == 0


def test_activity_preserves_issued_effect_while_profile_cancellation_waits(
    tmp_path,
) -> None:
    sessions, _run_switch, service, profile, application, adapter, _nodes = (
        _application(tmp_path)
    )
    assert service.tick() is True
    active = service.application(application.id).progress.switch_adapter
    assert active is not None and active.active_operation_id is not None
    child_id = active.active_operation_id
    adapter.request_cancellation = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    adapter.advance = lambda _application_id, **_kwargs: FleetProfileChildOperation(
        id=child_id, state="running"
    )  # type: ignore[method-assign]
    operations = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=service._clock,
        cursors=TokenCodec(b"b" * 32).cursor_codec(),
        operation_providers=(service.operation_provider(),),
    )
    api, tokens = _client(sessions, profiles=service, operations=operations)
    accepted = api.post(
        f"/api/profile/applications/{application.id}/cancel",
        json={"profile_number": profile.number, "request_key": _uuid(998)},
        headers=_headers(tokens, "administrator"),
    )
    assert accepted.status_code == 202, accepted.text

    activity = api.get(
        f"/api/operations/{application.id}",
        headers=_headers(tokens, "administrator"),
    )
    assert activity.status_code == 200, activity.text
    cancellation = activity.json()["cancellation"]
    assert activity.json()["state"] == "cancelling"
    assert cancellation["owner"] == "run-switch"
    assert cancellation["dependency"] == child_id
    assert cancellation["pending_effects"] == [
        {
            "effect_id": child_id,
            "kind": "run",
            "label": "Active Run/Switch child",
            "operation_id": child_id,
            "outcome": "pending",
        }
    ]


def test_cancel_during_child_start_keeps_late_start_receipt_from_resuming_profile(
    tmp_path, postgres_engine
) -> None:
    sessions, run_switch, service, profile, application, adapter, _nodes = _application(
        tmp_path, engine=postgres_engine
    )
    started = Event()
    resume_start = Event()
    original_start = adapter.start

    def delayed_start(**kwargs: Any):
        child = original_start(**kwargs)
        started.set()
        if not resume_start.wait(timeout=10):
            raise TimeoutError("test did not release the delayed child response")
        return child

    adapter.start = delayed_start  # type: ignore[method-assign]

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(service.tick)
        try:
            assert started.wait(timeout=10), "profile worker did not start its child"
            active = service.application(application.id).progress.switch_adapter
            assert active is not None and active.active_operation_id is not None
            active_operation_id = active.active_operation_id

            requested = service.cancel(
                application.id,
                profile_number=profile.number,
                request_key=_uuid(983),
                actor="admin",
            )
            assert requested.cancellation is not None
            assert requested.cancellation.state == "cancelling"
            assert requested.cancellation.owner == "run-switch"
            assert requested.cancellation.dependency == active_operation_id
            assert any(
                effect.operation_id == active_operation_id
                for effect in requested.cancellation.pending_effects
            )
            with sessions() as session:
                claims = tuple(
                    session.scalars(
                        select(ResourceReservation).where(
                            ResourceReservation.owner_kind == "fleet-profile",
                            ResourceReservation.owner_id == application.id,
                        )
                    )
                )
            assert claims
            assert all(claim.state in {"active", "promised"} for claim in claims)
        finally:
            resume_start.set()
        assert worker.result(timeout=10) is True

    # The start call returns its pre-cancellation running view after cancel
    # committed. That stale result may checkpoint identity, but cannot erase
    # the durable cancellation or make the parent dispatch another child.
    late = service.application(application.id)
    assert late.state == "running"
    assert late.cancellation is not None
    assert late.cancellation.state == "cancelling"
    assert late.current_operation_id == application.id

    assert service.tick() is True
    terminal = service.application(application.id)
    assert terminal.state == "cancelled"
    assert terminal.cancellation is not None
    assert terminal.cancellation.state == "cancelled"
    assert any(
        effect.operation_id == active_operation_id and effect.outcome == "cancelled"
        for effect in terminal.cancellation.cancelled_effects
    )
    with sessions() as session:
        run_switch_jobs = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        )
    assert len(run_switch_jobs) == 1
    assert run_switch.get(run_switch_jobs[0].id).state == "cancelled"


def test_pending_cancellation_observation_does_not_suppress_recovery(tmp_path) -> None:
    _sessions, run_switch, service, profile, application, adapter, _nodes = (
        _application(tmp_path)
    )
    assert service.tick() is True
    active = service.application(application.id).progress.switch_adapter
    assert active is not None and active.active_operation_id is not None
    child_id = active.active_operation_id

    # Keep the issued child unresolved so the cancellation remains pending;
    # the separate real-boundary test covers Run/Switch's cancellation owner.
    run_switch.cancel = lambda *_args, **_kwargs: run_switch.get(child_id)  # type: ignore[method-assign]
    service.cancel(
        application.id,
        profile_number=profile.number,
        request_key=_uuid(986),
        actor="admin",
    )

    observations: list[str] = []

    def pending_child(application_id: str, *, session) -> FleetProfileChildOperation:
        observations.append(application_id)
        return FleetProfileChildOperation(id=child_id, state="running")

    adapter.advance = pending_child  # type: ignore[method-assign]
    retries: list[str] = []
    service._automatic_cache_recovery = lambda _now: (application.id, "admin")  # type: ignore[method-assign]
    service.retry = lambda application_id, **_kwargs: retries.append(application_id)  # type: ignore[method-assign]

    # The actual scheduler boundary observes cancellation and still checks
    # unrelated recovery work during the same pass.
    assert service.tick() is True
    assert observations == [application.id]
    assert retries == [application.id]
    pending = service.application(application.id).cancellation
    assert pending is not None
    assert pending.state == "cancelling"
    assert pending.pending_effects[0].operation_id == child_id
    progress_cancellation = service.application(application.id).progress.cancellation
    assert progress_cancellation is not None
    assert progress_cancellation.observation_due_at is not None

    # The next worker pass can service other work instead of polling this
    # unresolved owner again before its next bounded observation time.
    service._automatic_cache_recovery = lambda _now: None  # type: ignore[method-assign]
    assert service.tick() is False
    assert observations == [application.id]


def test_cancellations_rotate_around_waiting_and_active_profile_applications(
    tmp_path,
    monkeypatch,
) -> None:
    def single_node_recipe(document: dict[str, Any]) -> None:
        topology = document["topology"]
        entrypoint = topology["roles"][0]
        entrypoint["count"] = 1
        topology.update(
            {
                "name": "solo",
                "mode": "single",
                "node_count": 1,
                "parallelism": {
                    "backend": "local",
                    "data": 1,
                    "pipeline": 1,
                    "tensor": 1,
                    "world_size": 1,
                },
                "fabric": {"connectivity": "none", "minimum_bandwidth_mbps": 0},
                "roles": [entrypoint],
                "start_order": ["entrypoint"],
                "stop_order": ["entrypoint"],
            }
        )
        document["models"][0]["files"][0]["roles"] = ["entrypoint"]

    original_preview = ClusterMappingService.preview

    def first_node_mapping(mappings, recipe_revision_id, node_ids, bindings, actor):
        return original_preview(
            mappings, recipe_revision_id, node_ids[:1], bindings, actor
        )

    with monkeypatch.context() as patcher:
        patcher.setattr(ClusterMappingService, "preview", first_node_mapping)
        sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
            tmp_path, nodes=3, recipe_transform=single_node_recipe
        )
    assert len(nodes) == 3
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    mappings = ClusterMappingService(sessions)
    for node_id in nodes[1:]:
        mapping = original_preview(mappings, revision.id, (node_id,), {}, "admin")
        mappings.materialize(mapping, actor="admin", now=lifecycle._clock())
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    start = lifecycle._clock()
    clock_now = {"now": start}
    service = build_production_fleet_profile_service(
        sessions,
        clock=lambda: clock_now["now"],
        run_switch_operations=run_switch,
    )
    adapter = service._switch_adapter
    assert isinstance(adapter, RunSwitchFleetProfileAdapter)

    applications = []
    for index, node_id in enumerate(nodes):
        clock_now["now"] = start.replace() + timedelta(seconds=index)
        profile = service.create(
            FleetProfileInput.model_validate(
                {
                    "name": f"Cancellation rotation {index}",
                    "assignments": [
                        {
                            "recipe_selector": f"vonk-forge/{revision.slug}",
                            "spark_ids": [node_id],
                            "desired_state": "running",
                            "assignment_name": f"cancel-rotation-{index}",
                        }
                    ],
                }
            ),
            actor="admin",
        )
        preview = service.preview(profile.id)
        assert preview.allowed is True
        application = service.load(
            profile.number,
            request_key=_uuid(990 + index),
            actor="admin",
            expected_plan_digest=preview.plan_digest,
        )
        applications.append((profile, application))

    clock_now["now"] = start + timedelta(seconds=3)
    assert service.tick() is True
    first_id = applications[0][1].id
    first_child = service.application(first_id).progress.switch_adapter
    assert first_child is not None and first_child.active_operation_id is not None

    active_child_ids = {first_id: first_child.active_operation_id}
    observations: list[str] = []
    adapter.request_cancellation = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    original_advance = adapter.advance

    def observe_pending(
        application_id: str, *, session=None
    ) -> FleetProfileChildOperation:
        child_id = active_child_ids.get(application_id)
        if child_id is not None:
            observations.append(application_id)
            return FleetProfileChildOperation(id=child_id, state="running")
        return original_advance(application_id, session=session)

    adapter.advance = observe_pending  # type: ignore[method-assign]
    first_profile, _ = applications[0]
    service.cancel(
        first_id,
        profile_number=first_profile.number,
        request_key=_uuid(993),
        actor="admin",
    )
    assert service.tick() is True
    assert observations == [first_id]

    second_profile, second_application = applications[1]
    second_id = second_application.id
    second_child = service.application(second_id).progress.switch_adapter
    assert second_child is not None and second_child.active_operation_id is not None
    active_child_ids[second_id] = second_child.active_operation_id
    with sessions.begin() as session:
        parked = session.get(FleetProfileApplication, second_id)
        assert parked is not None
        parked.state = "waiting-for-operator"
        parked.status_reason = "Synthetic owner wait before accepted cancellation"
    service.cancel(
        second_id,
        profile_number=second_profile.number,
        request_key=_uuid(994),
        actor="admin",
    )
    assert service.application(second_id).state == "running"
    # Model a late parked-child status racing with the accepted cancellation.
    # The parked observer must leave this row to its exact cancellation owner.
    with sessions.begin() as session:
        cancelling_parked = session.get(FleetProfileApplication, second_id)
        assert cancelling_parked is not None
        cancelling_parked.state = "waiting-for-operator"
        cancelling_parked.status_reason = "Late parked child observation"
    assert service.tick() is True
    assert observations == [first_id, second_id]
    second_pending = service.application(second_id).cancellation
    assert second_pending is not None
    assert second_pending.state == "cancelling"
    assert second_pending.pending_effects[0].operation_id == active_child_ids[second_id]

    third_id = applications[2][1].id
    third = service.application(third_id)
    third_switch = third.progress.switch_adapter
    assert third.state == "running"
    assert third_switch is not None and third_switch.active_operation_id is not None

    # Once both pending owners are due, each gets an observation before the
    # first can monopolize later scheduler passes; ordinary app three remains
    # eligible in the same passes.
    clock_now["now"] = start + timedelta(seconds=9)
    assert service.tick() is True
    assert observations == [first_id, second_id, first_id]
    assert service.tick() is True
    assert observations == [first_id, second_id, first_id, second_id]
    assert service.application(third_id).state == "running"
