"""Accepted serving ports survive preparation and transfer to the exact run."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import canonical_message
from vonk_control.fleet_profile_contract import FleetProfileApplicationProgress
from vonk_control.fleet_projection import FleetProjection
from vonk_control.models import (
    AgentNode,
    ClusterMapping,
    FleetProfileApplication,
    Job,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.recipe_operations import RecipeOperationConflict

from .test_profile_capacity_admission import _capacity_profile
from .test_profile_installed_execution import _drive_to_job, _profile_service
from .test_recipe_operations import (
    complete_started_recipe,
    installed_recipe,
    started_recipe,
)


def _ready_profile(tmp_path, engine, *, node_count=1):
    sessions, profiles, planner, profile, api, headers, _, nodes = _capacity_profile(
        tmp_path, engine, node_count=node_count
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    with sessions() as session:
        mapping_id = session.scalar(select(ClusterMapping.id))
        build_id = session.scalar(select(RecipeBuild.id))
        assert mapping_id is not None and build_id is not None
    installed = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4())
    )
    return sessions, profiles, planner, profile, api, headers, nodes, installed.owner_id


def _load(profile, api, headers):
    review = api.post(f"/api/profile/{profile.number}/preview", headers=headers).json()
    assert review["allowed"], review
    response = api.post(
        f"/api/profile/{profile.number}/load",
        headers=headers,
        json={
            "plan_digest": review["plan_digest"],
            "request_key": str(uuid4()),
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["id"], review


@pytest.mark.parametrize("node_count", [1, 2])
def test_profile_ports_block_competing_start_and_transfer_without_a_gap(
    tmp_path, postgres_engine, node_count
):
    sessions, profiles, planner, profile, api, headers, _, installation_id = (
        _ready_profile(tmp_path, postgres_engine, node_count=node_count)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    previously_fitting = lifecycle.preview_run(installation_id, "competing")
    assert previously_fitting.allowed
    application_id, review = _load(profile, api, headers)
    expected = {
        (node["node_id"], str(port))
        for decision in review["admission_decisions"]
        for node in decision["requirements"]
        for port in node["ports_required"]
    }
    with sessions() as session:
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "fleet-profile",
                    ResourceReservation.owner_id == application_id,
                    ResourceReservation.kind == "port",
                )
            )
        )
        assert {(claim.node_id, claim.resource_key) for claim in claims} == expected
        assert all(claim.state == "promised" for claim in claims)
        claim_ids = {claim.id for claim in claims}
    with pytest.raises(RecipeOperationConflict):
        lifecycle.start(
            previously_fitting,
            plan_digest=previously_fitting.plan_digest,
            actor="admin",
            request_id=str(uuid4()),
        )
    with sessions() as session:
        assert not tuple(session.scalars(select(RecipeRun)))
    competing = lifecycle.preview_run(installation_id, "competing")
    assert not competing.allowed
    assert any(
        "port_occupied" in reason.code
        for node in competing.nodes
        for reason in node.blockers
    )
    job_id = _drive_to_job(profiles, planner, sessions, "recipe.start")
    child = lifecycle.get(job_id)
    with sessions.begin() as session:
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(ResourceReservation.id.in_(claim_ids))
            )
        )
        assert {(claim.node_id, claim.resource_key) for claim in claims} == expected
        assert all(
            (claim.owner_kind, claim.owner_id, claim.state)
            == ("run", child.owner_id, "active")
            for claim in claims
        )
        application = session.get(FleetProfileApplication, application_id)
        assert application is not None
        application.progress = {"completed_steps": -1}
    assert profiles.tick()
    with sessions() as session:
        for claim_id in claim_ids:
            retained = session.get(ResourceReservation, claim_id)
            assert retained is not None and retained.state == "active"


@pytest.mark.parametrize("node_count", [1, 2])
def test_profile_port_promise_preserves_live_owner_and_survives_reviewed_stop(
    tmp_path, postgres_engine, node_count
):
    sessions, profiles, planner, profile, api, headers, nodes, installation_id = (
        _ready_profile(tmp_path, postgres_engine, node_count=node_count)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    old = started_recipe(
        sessions,
        lifecycle,
        installation_id,
        nodes,
        request_id=str(uuid4()),
        alias="previous",
    )
    application_id, review = _load(profile, api, headers)
    assert review["summary"]["stops"] == 1
    with sessions() as session:
        promised = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == application_id,
                    ResourceReservation.kind == "port",
                )
            )
        )
        old_ports = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == old.owner_id,
                    ResourceReservation.kind == "port",
                )
            )
        )
        assert promised and old_ports
        assert all(claim.state == "promised" for claim in promised)
        assert all(claim.state == "active" for claim in old_ports)
        counts = FleetProjection._reservations(session, nodes)
        for node_id in nodes:
            expected_ports = {
                claim.resource_key
                for claim in (*old_ports, *promised)
                if claim.node_id == node_id
            }
            assert counts[node_id]["port"][1] == len(expected_ports)
        promised_ids = {claim.id for claim in promised}
        old_ids = {claim.id for claim in old_ports}
    stop_id = _drive_to_job(profiles, planner, sessions, "recipe.stop")
    for node_id in nodes:
        lifecycle.record_node_result(
            stop_id, node_id, succeeded=True, evidence={"stopped": True}
        )
    # Even after the old owner releases its active port, unrelated work cannot
    # take the promised port in the gap before the replacement worker starts.
    competing = lifecycle.preview_run(installation_id, "competing")
    assert not competing.allowed
    assert any(
        "port_occupied" in reason.code
        for node in competing.nodes
        for reason in node.blockers
    )
    replacement = None
    for _ in range(16):
        planner.tick()
        profiles.tick()
        with sessions() as session:
            replacement = session.scalar(
                select(Job).where(Job.kind == "recipe.start", Job.id != old.id)
            )
        if replacement is not None:
            break
    assert replacement is not None
    child = lifecycle.get(replacement.id)
    with sessions() as session:
        for claim_id in old_ids:
            old_claim = session.get(ResourceReservation, claim_id)
            assert old_claim is not None and old_claim.state == "released"
        for claim_id in promised_ids:
            claim = session.get(ResourceReservation, claim_id)
            assert claim is not None
            assert (claim.owner_id, claim.state) == (child.owner_id, "active")


@pytest.mark.parametrize("completed_before_restart", [False, True])
def test_restart_adopts_committed_start_before_reassessing_its_owned_ports(
    tmp_path, postgres_engine, monkeypatch, completed_before_restart
):
    sessions, profiles, planner, profile, api, headers, _, installation_id = (
        _ready_profile(tmp_path, postgres_engine)
    )
    application_id, _ = _load(profile, api, headers)
    executor = planner._phase_executor
    assert executor is not None
    execute = executor.execute

    def crash_after_start(plan, phase, **kwargs):
        result = execute(plan, phase, **kwargs)
        if phase.kind == "start":
            raise SystemExit("child committed before parent checkpoint")
        return result

    monkeypatch.setattr(executor, "execute", crash_after_start)
    with pytest.raises(SystemExit, match="child committed"):
        for _ in range(16):
            planner.tick()
            profiles.tick()
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    with sessions() as session:
        child_id = session.scalar(select(Job.id).where(Job.kind == "recipe.start"))
        assert child_id is not None
        claim_ids = set(
            session.scalars(
                select(ResourceReservation.id).where(
                    ResourceReservation.kind.in_(("port", "unified-memory"))
                )
            )
        )
    if completed_before_restart:
        complete_started_recipe(sessions, lifecycle, child_id)
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.host_memory_free_bytes = snapshot.gpu_memory_free_bytes = 0
    assert not lifecycle.preview_run(installation_id, "unrelated").allowed
    profiles, planner = _profile_service(sessions, lifecycle)
    for _ in range(12):
        planner.tick()
        profiles.tick()
    child = lifecycle.get(child_id)
    with sessions() as session:
        assert tuple(
            session.scalars(select(Job.id).where(Job.kind == "recipe.start"))
        ) == (child_id,)
        application = session.get(FleetProfileApplication, application_id)
        assert application is not None
        # This fixture has no gateway publisher. Completed starts must reconnect
        # and advance to final verification, which still waits for publication.
        assert application.state == "running"
        parent_id = session.scalar(
            select(Job.id).where(Job.kind == "recipe.run-switch.v2")
        )
        assert parent_id is not None
        parent = planner.get(parent_id)
        assert parent.result is not None
        assert parent.result.phase == (
            "final_verify" if completed_before_restart else "start"
        )
        for claim_id in claim_ids:
            claim = session.get(ResourceReservation, claim_id)
            assert (
                claim is not None
                and claim.state == "active"
                and claim.owner_id == child.owner_id
            )


@pytest.mark.parametrize("change", ["released", "amount", "intent"])
def test_run_cannot_consume_changed_profile_port_authority(
    tmp_path, postgres_engine, change
):
    sessions, _, planner, profile, api, headers, nodes, installation_id = (
        _ready_profile(tmp_path, postgres_engine)
    )
    application_id, review = _load(profile, api, headers)
    alias = review["admission_decisions"][0]["alias"]
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    plan = lifecycle.preview_run(
        installation_id, alias, profile_application_id=application_id
    )
    assert plan.allowed
    with sessions.begin() as session:
        application = session.get(FleetProfileApplication, application_id)
        assert application is not None
        ordinal = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(application.progress), strict=True
        ).workload_intent_ordinal
        assert ordinal is not None
        claim = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == application_id,
                ResourceReservation.kind == "port",
            )
        )
        assert claim is not None
        if change == "released":
            claim.state = "released"
        elif change == "amount":
            claim.amount_bytes = 1
        else:
            node = session.get(AgentNode, nodes[0])
            assert node is not None
            node.workload_intent_ordinal += 1
    with pytest.raises(RecipeOperationConflict):
        lifecycle.start(
            plan,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=str(uuid4()),
            profile_application_id=application_id,
            workload_intent_ordinal=ordinal,
        )
    with sessions() as session:
        assert not tuple(session.scalars(select(RecipeRun)))


@pytest.mark.parametrize("kind", ["port", "unified-memory"])
def test_busy_runtime_handoff_retries_the_original_claim_after_releasing_sql(
    tmp_path, postgres_engine, kind
):
    sessions, profiles, planner, profile, api, headers, _, _ = _ready_profile(
        tmp_path, postgres_engine
    )
    application_id, _ = _load(profile, api, headers)
    assert profiles.tick()
    with sessions() as session:
        operation_id = session.scalar(
            select(Job.id).where(Job.kind == "recipe.run-switch.v2")
        )
        assert operation_id is not None
    with sessions.begin() as holder:
        claim = holder.scalar(
            select(ResourceReservation)
            .where(
                ResourceReservation.owner_id == application_id,
                ResourceReservation.kind == kind,
            )
            .with_for_update()
        )
        assert claim is not None
        claim_id = claim.id
        for _ in range(12):
            planner.tick()
            profiles.tick()
            operation = planner.get(operation_id)
            if (
                operation.result is not None
                and operation.result.retry_reason == "run.capacity_busy"
            ):
                break
        assert operation.state == "running"
        assert (
            operation.result is not None
            and operation.result.retry_reason == "run.capacity_busy"
        )
        due = operation.result.observation_due_at
        assert due is not None
        assert not planner.tick()
        with sessions() as observer:
            assert not tuple(observer.scalars(select(RecipeRun)))
    after_due = (
        due if isinstance(due, datetime) else datetime.fromisoformat(due)
    ) + timedelta(seconds=1)
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    planner._clock = profiles._clock = lifecycle._clock = lambda: after_due
    child_id = _drive_to_job(profiles, planner, sessions, "recipe.start")
    child = lifecycle.get(child_id)
    with sessions() as session:
        claim = session.get(ResourceReservation, claim_id)
        assert claim is not None
        assert (claim.owner_id, claim.state) == (child.owner_id, "active")


def test_new_profile_intent_releases_only_the_previous_port_promise(
    tmp_path, postgres_engine
):
    sessions, _, _, profile, api, headers, _, _ = _ready_profile(
        tmp_path, postgres_engine
    )
    previous, _ = _load(profile, api, headers)
    current, _ = _load(profile, api, headers)
    with sessions() as session:
        previous_claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == previous
                )
            )
        )
        current_claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == current
                )
            )
        )
        assert previous_claims and all(
            claim.state == "released" for claim in previous_claims
        )
        assert current_claims and all(
            claim.state == "promised" for claim in current_claims
        )
