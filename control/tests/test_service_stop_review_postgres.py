"""Accepted service Stop review survives route withdrawal and worker restart."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import canonical_message
from vonk_control.categorized_errors import InvalidRequestError
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Job,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_operations import RecipeOperationService

from .test_recipe_operations import (
    NOW,
    ConcurrentPublisher,
    bind_route_publications,
    installed_recipe,
    mark_current_exact_observations,
    setup_services,
    started_recipe,
)


@pytest.fixture
def published_service_run(postgres_engine, tmp_path):
    sessions, service, jobs, mapping, build, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    installation = installed_recipe(
        service, mapping, build, nodes, request_id=str(uuid4())
    )
    run = started_recipe(
        sessions, service, installation.owner_id, nodes, request_id=str(uuid4())
    )
    publisher = ConcurrentPublisher()
    service, routes = bind_route_publications(sessions, service, publisher)
    routes.publish_run(run.owner_id)
    return sessions, service, jobs, routes, publisher, run.owner_id, nodes


def claims(sessions, run_id):
    with sessions() as session:
        return {
            (row.id, row.node_id, row.state, row.amount_bytes)
            for row in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                )
            )
        }


@pytest.mark.parametrize("changed", [False, True])
def test_unreviewed_service_stop_has_no_publication_or_admission_effect(
    published_service_run, changed
):
    sessions, service, _jobs, _routes, publisher, run_id, nodes = published_service_run
    preview = service.preview_stop(run_id)
    before_claims = claims(sessions, run_id)
    before_generation = len(publisher.aliases)
    with sessions() as session:
        before_ordinals = {
            node.node_id: node.workload_intent_ordinal
            for node in session.scalars(
                select(AgentNode).where(AgentNode.node_id.in_(nodes))
            )
        }
    if changed:
        # A real state change produces a different preview, not an invented DTO.
        with sessions.begin() as session:
            run = session.get(RecipeRun, run_id)
            assert run is not None
            run.state = "failed"
        assert service.preview_stop(run_id).plan_digest != preview.plan_digest
    request = str(uuid4())
    with pytest.raises(InvalidRequestError):
        service.stop(
            run_id,
            plan_digest=preview.plan_digest if changed else "0" * 64,
            actor="admin",
            request_id=request,
        )
    assert len(publisher.aliases) == before_generation
    assert claims(sessions, run_id) == before_claims
    with sessions() as session:
        assert session.scalar(select(Job.id).where(Job.request_id == request)) is None
        assert {
            node.node_id: node.workload_intent_ordinal
            for node in session.scalars(
                select(AgentNode).where(AgentNode.node_id.in_(nodes))
            )
        } == before_ordinals


def test_accepted_service_stop_resumes_same_review_after_withdrawal_restart(
    published_service_run, monkeypatch
):
    sessions, service, jobs, routes, publisher, run_id, nodes = published_service_run
    preview = service.preview_stop(run_id)
    before_claims = claims(sessions, run_id)
    request = str(uuid4())

    def process_lost(*args, **kwargs):
        raise RuntimeError("process lost after route withdrawal")

    monkeypatch.setattr(service, "_dispatch_stop_after_withdrawal", process_lost)
    with pytest.raises(RuntimeError, match="process lost"):
        service.stop(
            run_id, plan_digest=preview.plan_digest, actor="admin", request_id=request
        )
    with sessions() as session:
        accepted = session.scalar(select(Job).where(Job.request_id == request))
        run = session.get(RecipeRun, run_id)
        assert accepted is not None and run is not None
        accepted_id = accepted.id
        retained = canonical_message(
            accepted.payload["service_stop_review"]["exact_payloads"]
        )
        ordinal = accepted.payload["workload_intent_ordinal"]
        assert accepted.state == "running" and accepted.payload.get("phases") is None
        assert accepted.payload["plan_digest"] == preview.plan_digest
        assert accepted.payload["service_stop_review"]["stage"] == "withdrawal-claimed"
        assert run.route_state == "withdrawn" and run.state == "running"
        assert (
            session.scalar(
                select(AgentOperation.id).where(
                    AgentOperation.parent_job_id == accepted_id
                )
            )
            is None
        )
    assert claims(sessions, run_id) == before_claims
    assert service.get(accepted_id).state == "running"
    # The service's own route withdrawal changes the current public preview.
    assert service.preview_stop(run_id).plan_digest != preview.plan_digest
    # Once the old abandoned-withdrawal grace expires, the accepted exact Stop
    # still owns this route. Fresh native observations are not fresh consent to
    # re-publish it while its durable Stop awaits dispatch.
    clock = NOW + timedelta(minutes=6)
    mark_current_exact_observations(sessions, run_id, clock)
    routes._clock = lambda: clock
    generations = len(publisher.aliases)
    routes.maintain()
    assert len(publisher.aliases) == generations
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.route_state == "withdrawn"
    restarted = RecipeOperationService(
        sessions,
        install_admission=service._install_admission,
        run_admission=service._run_admission,
        agent_jobs=jobs,
        route_publications=routes,
        clock=lambda: clock,
    )
    worker = RecipeOperationWorker(
        sessions,
        routes,
        clock=lambda: clock,
        stop_admission_cleanup=restarted.reconcile_pending_service_stops,
    )
    worker.tick()
    with sessions() as session:
        accepted = session.get(Job, accepted_id)
        assert accepted is not None and accepted.state == "running"
        assert accepted.payload["plan_digest"] == preview.plan_digest
        assert accepted.payload["workload_intent_ordinal"] == ordinal
        assert accepted.payload["service_stop_review"]["stage"] == "dispatched"
        assert (
            canonical_message(accepted.payload["service_stop_review"]["exact_payloads"])
            == retained
        )
        native = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == accepted_id
                )
            )
        )
        assert native and {row.node_id for row in native} <= set(nodes)
        manifest = {(row.id, row.payload_digest) for row in native}
    generation = len(publisher.aliases)
    replay = restarted.stop(
        run_id, plan_digest=preview.plan_digest, actor="admin", request_id=request
    )
    assert replay.id == accepted_id
    with pytest.raises(InvalidRequestError):
        restarted.stop(run_id, plan_digest="0" * 64, actor="admin", request_id=request)
    with sessions() as session:
        assert {
            (row.id, row.payload_digest)
            for row in session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == accepted_id
                )
            )
        } == manifest
        assert (
            session.scalar(select(Job.id).where(Job.request_id == request))
            == accepted_id
        )
    assert len(publisher.aliases) == generation
    assert claims(sessions, run_id) == before_claims


@pytest.mark.parametrize("change", ["newer-intent", "runtime-generation"])
def test_pending_service_stop_cannot_resume_under_changed_authority(
    published_service_run, monkeypatch, change
):
    sessions, service, jobs, routes, publisher, run_id, nodes = published_service_run
    preview = service.preview_stop(run_id)
    request = str(uuid4())
    before_claims = claims(sessions, run_id)

    def process_lost(*args, **kwargs):
        raise RuntimeError("process lost after route withdrawal")

    monkeypatch.setattr(service, "_dispatch_stop_after_withdrawal", process_lost)
    with pytest.raises(RuntimeError, match="process lost"):
        service.stop(
            run_id, plan_digest=preview.plan_digest, actor="admin", request_id=request
        )
    with sessions.begin() as session:
        accepted = session.scalar(select(Job).where(Job.request_id == request))
        assert accepted is not None
        original_payload = canonical_message(accepted.payload)
        accepted_id = accepted.id
        if change == "newer-intent":
            for node_id in nodes:
                node = session.get(AgentNode, node_id)
                assert node is not None
                node.workload_intent_ordinal += 1
        else:
            run = session.get(RecipeRun, run_id)
            assert run is not None
            run.run_generation += 1
    before_publications = len(publisher.aliases)
    clock = NOW + timedelta(seconds=6)
    restarted = RecipeOperationService(
        sessions,
        install_admission=service._install_admission,
        run_admission=service._run_admission,
        agent_jobs=jobs,
        route_publications=routes,
        clock=lambda: clock,
    )
    RecipeOperationWorker(
        sessions,
        routes,
        clock=lambda: clock,
        stop_admission_cleanup=restarted.reconcile_pending_service_stops,
    ).tick()
    with sessions() as session:
        accepted = session.get(Job, accepted_id)
        assert accepted is not None and accepted.state == "running"
        assert canonical_message(accepted.payload) == original_payload
        assert accepted.status_reason is not None
        assert (
            "deferred" in accepted.status_reason
            and "next reconciliation" in accepted.status_reason
        )
        assert (
            session.scalar(
                select(AgentOperation.id).where(
                    AgentOperation.parent_job_id == accepted_id
                )
            )
            is None
        )
    assert len(publisher.aliases) == before_publications
    assert claims(sessions, run_id) == before_claims


@pytest.mark.parametrize("replacement", ["adopt", "cancel"])
def test_profile_adopts_exact_accepted_stop_before_native_dispatch(
    postgres_engine, tmp_path, monkeypatch, replacement
):
    from vonk_agent_protocol import (
        AgentResult,
        AgentResultState,
        OutcomeDone,
        OutcomeKind,
        RecipeStopResult,
    )
    from vonk_control.agent_jobs import AgentJobService
    from vonk_control.fleet_profiles import (
        RunSwitchFleetProfileAdapter,
        _persisted_profile_plan,
    )
    from vonk_control.models import FleetProfileApplication

    from .agent_fences import fenced_operation
    from .test_fleet_profiles import _uuid
    from .test_profile_adapter_parallel_postgres import _stored
    from .test_profile_stop_effect_adoption_postgres import _pending_stop
    from .test_recipe_operations import _issue_exact_stop_grant, complete_started_recipe
    from .test_run_switch_operations import RecordingArtifactExecutor, _service

    route_owner = []
    publishers = []
    blocked = [True]

    def freeze_after_withdrawal(sessions, lifecycle, clock, run_id):
        publisher = ConcurrentPublisher()
        _bound, routes = bind_route_publications(sessions, lifecycle, publisher)
        routes._clock = lambda: clock[0]
        lifecycle._route_publications = routes
        routes.publish_run(run_id)
        route_owner.append(routes)
        publishers.append(publisher)
        original_dispatch = lifecycle._dispatch_stop_after_withdrawal

        def dispatch(*args, **kwargs):
            if blocked[0]:
                from vonk_control.recipe_operations import RecipeRetryLater

                raise RecipeRetryLater(
                    "injected unavailable dispatch after exact withdrawal"
                )
            return original_dispatch(*args, **kwargs)

        monkeypatch.setattr(lifecycle, "_dispatch_stop_after_withdrawal", dispatch)

    (
        sessions,
        profiles,
        profile,
        clock,
        original,
        lifecycle,
        _old_jobs,
        nodes,
        stop_id,
        request_key,
        stop_index,
        native_id,
        claim,
        profile_digest,
        ordinal,
        healthy_child,
    ) = _pending_stop(
        postgres_engine,
        tmp_path,
        monkeypatch,
        lifecycle_before_dispatch=freeze_after_withdrawal,
        issue_grant=False,
    )
    assert native_id is None and claim is None
    before_claims = claims(
        sessions, _stored(sessions, original.id).queue[stop_index].id
    )
    assert before_claims
    with sessions() as session:
        accepted = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string()
                == _stored(sessions, original.id).queue[stop_index].id,
            )
        )
        assert accepted is not None and accepted.state == "running"
        accepted_id, accepted_key = accepted.id, accepted.request_id
        reviewed_digest = accepted.payload["plan_digest"]
        reviewed_payloads = canonical_message(
            accepted.payload["service_stop_review"]["exact_payloads"]
        )
        assert (
            accepted.payload["service_stop_review"]["profile_stop_owner"][
                "profile_operation_id"
            ]
            == stop_id
        )
        assert accepted.payload["service_stop_review"]["stage"] == "withdrawal-claimed"
        run_id = accepted.payload["owner_id"]
        run = session.get(RecipeRun, run_id)
        assert run is not None
        installation_id = run.installation_id
    for index in range(2):
        desired = profile(f"pre-dispatch-adoption-{index}")
        preview = profiles.preview(desired.id)
        [link] = [
            item
            for item in preview.effects.adopted
            if item.application_id == original.id
        ]
        assert (
            link.plan_digest == profile_digest
            and link.workload_intent_ordinal == ordinal
        )
        assert link.node_ids == [nodes[0]] and link.assignment_ids == []
        [effect] = link.stops
        assert effect.operation_id == stop_id and effect.request_key == request_key
        assert effect.queue_index == stop_index and effect.effect.run_id == run_id
        assert effect.effect.node_ids == [nodes[0]]
        selected = profiles.apply(
            desired.id, request_key=_uuid(19710 + index), actor="admin"
        )
        with sessions() as session:
            selected_row = session.get(FleetProfileApplication, selected.id)
            assert selected_row is not None
            assert (
                _persisted_profile_plan(selected_row).effects.adopted
                == preview.effects.adopted
            )
            node = session.get(AgentNode, nodes[0])
            assert node is not None and node.workload_intent_ordinal == ordinal
    if replacement == "cancel":
        profiles.cancel(
            selected.id,
            profile_number=desired.number,
            request_key=_uuid(19720),
            actor="admin",
        )
    clock[0] += timedelta(minutes=6)
    mark_current_exact_observations(sessions, run_id, clock[0])
    [routes], [publisher] = route_owner, publishers
    count = len(publisher.aliases)
    routes.maintain()
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.route_state == "withdrawn"
    assert len(publisher.aliases) == count
    blocked[0] = False
    restarted = RecipeOperationService(
        sessions,
        install_admission=lifecycle._install_admission,
        run_admission=lifecycle._run_admission,
        agent_jobs=lifecycle._agent_jobs,
        route_publications=routes,
        clock=lambda: clock[0],
    )
    RecipeOperationWorker(
        sessions,
        routes,
        clock=lambda: clock[0],
        stop_admission_cleanup=restarted.reconcile_pending_service_stops,
    ).tick()
    with sessions() as session:
        accepted = session.get(Job, accepted_id)
        assert accepted is not None
        assert (
            accepted.request_id == accepted_key
            and accepted.payload["plan_digest"] == reviewed_digest
        )
        assert (
            canonical_message(accepted.payload["service_stop_review"]["exact_payloads"])
            == reviewed_payloads
        )
        native = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == accepted_id
                )
            )
        )
        if replacement == "cancel":
            assert not native and accepted.state == "running"
            assert (
                accepted.payload["service_stop_review"]["stage"] == "withdrawal-claimed"
            )
            assert (
                accepted.status_reason is not None
                and "deferred" in accepted.status_reason
            )
            assert claims(sessions, run_id) == before_claims
            return
        assert len(native) == 1
        native_id = native[0].id
        assert accepted.payload["service_stop_review"]["stage"] == "dispatched"
    jobs = AgentJobService(sessions, clock=lambda: clock[0])
    jobs.set_result_consumer(restarted.consume_agent_result)
    fresh, stop, _grant = _issue_exact_stop_grant(
        sessions, node_id=nodes[0], certificate_serial="serial-0", grant_now=clock[0]
    )
    assert fenced_operation(sessions, fresh).id == native_id and stop.run_id == run_id
    jobs.record_result(
        AgentResult(
            fence=fresh.fence,
            state=AgentResultState.SUCCEEDED,
            result=OutcomeDone(kind=OutcomeKind.DONE, result=RecipeStopResult()),
        )
    )
    coordinator = _service(sessions, clock[0], restarted, RecordingArtifactExecutor())
    adapter = RunSwitchFleetProfileAdapter(sessions, coordinator)
    native_observe = adapter._observed_child
    monkeypatch.setattr(
        adapter,
        "_observed_child",
        lambda identity: (
            healthy_child
            if identity == healthy_child.operation_id
            else native_observe(identity)
        ),
    )
    for _ in range(3):
        coordinator.tick()
        adapter.advance(original.id)
    assert restarted.get(accepted_id).state == "succeeded"
    assert any(
        child.queue_index == stop_index
        and child.operation_id == stop_id
        and child.state == "succeeded"
        for child in _stored(sessions, original.id).children
    )
    assert not any(
        state == "active" for _id, _node, state, _bytes in claims(sessions, run_id)
    )
    # The acknowledged Stop releases real runtime capacity for a fresh request.
    fresh_plan = restarted.preview_run(installation_id, "after-adopted-stop")
    assert fresh_plan.allowed
    fresh_run = restarted.start(
        fresh_plan,
        plan_digest=fresh_plan.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )
    complete_started_recipe(sessions, restarted, fresh_run.id)
    assert restarted.get(fresh_run.id).state == "succeeded"


def test_lost_service_start_history_exact_stop_restart_releases_fresh_run(
    published_service_run, monkeypatch
):
    from vonk_agent_protocol import (
        AgentResult,
        AgentResultState,
        OutcomeDone,
        OutcomeKind,
        RecipeStopResult,
    )
    from vonk_control.agent_jobs import AgentJobService

    from .agent_fences import fenced_operation
    from .test_recipe_operations import _issue_exact_stop_grant, complete_started_recipe

    sessions, service, _jobs, routes, publisher, run_id, nodes = published_service_run
    before_claims = claims(sessions, run_id)
    assert any(state == "active" for _id, _node, state, _bytes in before_claims)
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        installation_id = run.installation_id
        accepted_run_digest = run.plan_digest
        accepted_generation = run.run_generation
        history = session.scalar(
            select(Job).where(
                Job.kind == "recipe.start",
                Job.payload["owner_id"].as_string() == run_id,
            )
        )
        assert history is not None and history.state == "succeeded"
        lost_start_id = history.id
        session.delete(history)
        for member in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            # Lost observation is neither a process absence nor released memory.
            member.observed_run_generation = None
            member.observation_process_running = None
            member.observation_endpoint_ready = None
            member.observation_observed_at = None
    generations = len(publisher.aliases)
    routes.maintain()
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.route_state == "published"
        assert session.get(Job, lost_start_id) is None
    assert len(publisher.aliases) == generations
    assert claims(sessions, run_id) == before_claims
    # Real allocator admission still accounts for this uncertain service run.
    assert not service.preview_run(installation_id, "before-exact-cleanup").allowed
    preview = service.preview_stop(run_id)
    assert preview.allowed
    request = str(uuid4())

    def process_lost(*args, **kwargs):
        raise RuntimeError("lost service Stop process after accepted withdrawal")

    monkeypatch.setattr(service, "_dispatch_stop_after_withdrawal", process_lost)
    with pytest.raises(RuntimeError, match="lost service Stop"):
        service.stop(
            run_id, plan_digest=preview.plan_digest, actor="admin", request_id=request
        )
    with sessions() as session:
        accepted = session.scalar(select(Job).where(Job.request_id == request))
        assert accepted is not None and accepted.state == "running"
        accepted_id = accepted.id
        exact_payloads = canonical_message(
            accepted.payload["service_stop_review"]["exact_payloads"]
        )
        assert accepted.payload.get("phases") is None
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.route_state == "withdrawn"
    assert claims(sessions, run_id) == before_claims
    clock = NOW + timedelta(seconds=6)
    jobs = AgentJobService(sessions, clock=lambda: clock)
    restarted = RecipeOperationService(
        sessions,
        install_admission=service._install_admission,
        run_admission=service._run_admission,
        agent_jobs=jobs,
        route_publications=routes,
        clock=lambda: clock,
    )
    jobs.set_result_consumer(restarted.consume_agent_result)
    RecipeOperationWorker(
        sessions,
        routes,
        clock=lambda: clock,
        stop_admission_cleanup=restarted.reconcile_pending_service_stops,
    ).tick()
    assert (
        restarted.stop(
            run_id, plan_digest=preview.plan_digest, actor="admin", request_id=request
        ).id
        == accepted_id
    )
    with sessions() as session:
        accepted = session.get(Job, accepted_id)
        assert accepted is not None
        assert (
            canonical_message(accepted.payload["service_stop_review"]["exact_payloads"])
            == exact_payloads
        )
    assert claims(sessions, run_id) == before_claims
    claim, stop, _grant = _issue_exact_stop_grant(
        sessions, node_id=nodes[0], certificate_serial="serial-0", grant_now=clock
    )
    assert stop.run_id == run_id and stop.run_generation == accepted_generation
    assert stop.plan_digest == accepted_run_digest
    assert fenced_operation(sessions, claim).parent_job_id == accepted_id
    jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.SUCCEEDED,
            result=OutcomeDone(kind=OutcomeKind.DONE, result=RecipeStopResult()),
        )
    )
    assert restarted.get(accepted_id).state == "succeeded"
    assert not any(
        state == "active" for _id, _node, state, _bytes in claims(sessions, run_id)
    )
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == "stopped"
    fresh_plan = restarted.preview_run(installation_id, "after-lost-service-cleanup")
    assert fresh_plan.allowed
    fresh = restarted.start(
        fresh_plan,
        plan_digest=fresh_plan.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )
    assert fresh.owner_id != run_id
    complete_started_recipe(sessions, restarted, fresh.id)
    assert restarted.get(fresh.id).state == "succeeded"
