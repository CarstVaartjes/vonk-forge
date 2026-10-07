"""Reviewed parked receipts and uncertain effects never starve newer authority.

The three scenarios recover the intent of the historical newer-intent-cleanup
proofs without treating damaged evidence as failure or cancellation as proof of
physical absence. All database execution belongs to hosted PostgreSQL CI.
"""

from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    LifecycleState,
    OutcomeKind,
    OutcomeUnknown,
    WaitReason,
)
from vonk_agent_protocol.recipe_operations import RecipeStopPayload
from vonk_control import agent_operation_states
from vonk_control.agent_jobs import AgentJobService
from vonk_control.fleet_profile_contract import FleetProfileInput, FleetProfilePreview
from vonk_control.fleet_profiles import (
    _persisted_profile_plan,
    build_production_fleet_profile_service,
)
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models import (
    FleetProfileApplication,
    Job,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.run_switch_operations import RunSwitchOperationService
from vonk_control.stored_json import write_guard_mode

from .agent_fences import fenced_attempt, fenced_operation
from .test_fleet_profile_cancel import (
    _agent_result,
    _agent_service_and_target_claim,
    _start_profile_stop_child,
    _two_target_stop_case,
)
from .test_fleet_profiles import _follow_profile_retry, _uuid
from .test_recipe_operations import NOW, installed_recipe, setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)


def _superseded(plan: FleetProfilePreview | Residue):
    assert isinstance(plan, FleetProfilePreview), plan
    return {
        (effect.kind, effect.id, tuple(effect.node_ids))
        for effect in plan.effects.superseded
    }


def _held_run_claims(sessions, run_id):
    with sessions() as session:
        return {
            (row.id, row.node_id, row.kind, row.state)
            for row in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                    ResourceReservation.state == "active",
                )
            )
        }


def test_postgres_reviewed_parked_receipt_remains_bound_when_restarted_new_load_retires_it(
    tmp_path,
    postgres_engine,
):
    (sessions, _lifecycle, switches, profiles, profile, original, _run, nodes, now) = (
        _two_target_stop_case(tmp_path, engine=postgres_engine)
    )
    original_review = profiles.preview(profile.id)
    parked = profiles._create_pending_application(
        original_review,
        request_key=_uuid(18800),
        actor="admin",
        operation_kind="fleet-profile.apply",
    )
    with sessions() as session:
        row = session.get(FleetProfileApplication, parked.id)
        assert row is not None
        parked_key, parked_digest, parked_plan = (
            row.request_key,
            row.plan_digest,
            deepcopy(row.plan),
        )
    restarted = build_production_fleet_profile_service(
        sessions, clock=lambda: now[0], run_switch_operations=switches
    )
    reviewed = restarted.preview(profile.id)
    expected = {
        ("profile-application", original.id, tuple(nodes)),
        ("profile-application", parked.id, tuple(nodes)),
    }
    assert expected <= _superseded(reviewed)
    assert reviewed.effects_digest is not None
    replacement = restarted.load(
        profile.number,
        request_key=_uuid(18801),
        actor="admin",
        reviewed_effects_digest=reviewed.effects_digest,
    )
    assert replacement.id not in {original.id, parked.id}
    with sessions() as session:
        accepted = session.get(FleetProfileApplication, replacement.id)
        retained = session.get(FleetProfileApplication, parked.id)
        assert accepted is not None and retained is not None
        accepted_plan = _persisted_profile_plan(accepted)
        assert isinstance(accepted_plan, FleetProfilePreview), accepted_plan
        assert expected <= _superseded(accepted_plan)
        assert all(
            effect.id != replacement.id for effect in accepted_plan.effects.superseded
        )
        assert (
            retained.request_key == parked_key and retained.plan_digest == parked_digest
        )
        assert retained.plan == parked_plan
        assert retained.state == LifecycleState.SUPERSEDED
    replay = restarted.load(profile.number, request_key=_uuid(18801), actor="admin")
    assert replay.id == replacement.id


def test_postgres_unrelated_damaged_parked_evidence_does_not_starve_exact_gang_cleanup(
    tmp_path,
    postgres_engine,
):
    (sessions, lifecycle, switches, profiles, profile, _original, run, nodes, now) = (
        _two_target_stop_case(tmp_path, engine=postgres_engine)
    )
    parked = profiles._create_pending_application(
        profiles.preview(profile.id),
        request_key=_uuid(18810),
        actor="admin",
        operation_kind="fleet-profile.apply",
    )
    unrelated = "spk_" + "f" * 32
    with write_guard_mode(strict=False), sessions.begin() as session:
        row = session.get(FleetProfileApplication, parked.id)
        assert row is not None
        damaged = {
            **row.plan,
            "scope": {"node_ids": [unrelated]},
            "steps": [{"kind": "not-a-step"}],
        }
        row.plan = damaged
        retained_key, retained_digest = row.request_key, row.plan_digest
    jobs = AgentJobService(
        sessions, clock=lambda: now[0], result_consumer=lifecycle.consume_agent_result
    )
    lifecycle._agent_jobs = jobs
    restarted = build_production_fleet_profile_service(
        sessions, clock=lambda: now[0], run_switch_operations=switches
    )
    reviewed = restarted.preview(profile.id)
    assert reviewed.allowed, reviewed.reasons
    assert all(effect.id != parked.id for effect in reviewed.effects.superseded)
    assert reviewed.effects_digest is not None
    replacement = restarted.load(
        profile.number,
        request_key=_uuid(18811),
        actor="admin",
        reviewed_effects_digest=reviewed.effects_digest,
    )
    before = _held_run_claims(sessions, run.owner_id)
    assert before and {claim[1] for claim in before} == set(nodes)
    _child, stop_job = _start_profile_stop_child(
        sessions, switches, restarted, replacement
    )
    assert _held_run_claims(sessions, run.owner_id) == before
    with sessions() as session:
        retained = session.get(FleetProfileApplication, parked.id)
        assert retained is not None
        assert retained.plan == damaged
        assert (
            retained.request_key == retained_key
            and retained.plan_digest == retained_digest
        )
        assert retained.state not in {LifecycleState.FAILED, LifecycleState.SUCCEEDED}
        assert "unknown" in (retained.status_reason or "")
    # No repair or terminal receipt is invented for the damaged sibling. The
    # actual unrelated gang cleanup can still claim both of its exact Stops.
    for index, node_id in enumerate(nodes):
        _, claim = _agent_service_and_target_claim(
            sessions,
            lifecycle,
            node_id,
            nodes,
            clock=lambda: now[0],
            jobs=jobs,
        )
        assert claim is not None and claim.operation.value == "recipe.stop"
        assert fenced_operation(sessions, claim).parent_job_id == stop_job
        stop = RecipeStopPayload.model_validate(claim.payload)
        assert stop.run_id == run.owner_id and stop.target_runtime_id == run.owner_id
        jobs.record_result(_agent_result(claim, state="succeeded", result={}))
        if index == 0:
            assert _held_run_claims(sessions, run.owner_id), (
                "one rank is not a stopped gang"
            )
    for _ in range(8):
        _follow_profile_retry(restarted, replacement.id, now)
        switches.tick()
        restarted.tick()
    assert restarted.application(replacement.id).state == LifecycleState.SUCCEEDED
    assert not _held_run_claims(sessions, run.owner_id)
    with sessions() as session:
        retained = session.get(FleetProfileApplication, parked.id)
        assert retained is not None and retained.plan == damaged
        assert retained.state != LifecycleState.FAILED


def test_postgres_unknown_old_start_is_reviewed_but_only_exact_stop_releases_gang_claims(
    tmp_path,
    postgres_engine,
):
    now = [NOW]
    sessions, lifecycle, _queue, mapping, build, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    lifecycle._clock = lambda: now[0]
    installation = installed_recipe(
        lifecycle, mapping, build, nodes, request_id=_uuid(18820)
    )
    jobs = AgentJobService(
        sessions, clock=lambda: now[0], result_consumer=lifecycle.consume_agent_result
    )
    lifecycle._agent_jobs = jobs
    plan = lifecycle.preview_run(installation.owner_id, "unknown-old-start")
    old_start = lifecycle.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=_uuid(18821)
    )
    old_claim = None
    for node_id in nodes:
        _, candidate = _agent_service_and_target_claim(
            sessions,
            lifecycle,
            node_id,
            nodes,
            clock=lambda: now[0],
            jobs=jobs,
        )
        if candidate is not None:
            old_claim = candidate
            break
    assert old_claim is not None and old_claim.operation.value == "recipe.start"
    jobs.record_result(
        AgentResult(
            fence=old_claim.fence,
            state=AgentResultState.OBSERVING,
            result=OutcomeUnknown(
                kind=OutcomeKind.UNKNOWN,
                wait_reason=WaitReason.OBSERVATION_UNAVAILABLE,
                reason="Start effect receipt unavailable",
            ),
        )
    )
    attempt = fenced_attempt(sessions, old_claim)
    assert agent_operation_states.attempt_reported_unknown(attempt)
    original_evidence = deepcopy(attempt.result)
    old_operation = fenced_operation(sessions, old_claim)
    original_payload = deepcopy(old_operation.payload)
    switches = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: now[0],
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    profiles = build_production_fleet_profile_service(
        sessions, clock=lambda: now[0], run_switch_operations=switches
    )
    profile = profiles.create(
        FleetProfileInput(name="Retire uncertain old Start", assignments=[]),
        actor="admin",
    )
    reviewed = profiles.preview(profile.id)
    assert reviewed.allowed, reviewed.reasons
    assert ("job", old_start.id, tuple(nodes)) in _superseded(reviewed)
    run_id = old_start.owner_id
    claims = _held_run_claims(sessions, run_id)
    assert claims and {claim[1] for claim in claims} == set(nodes)
    assert reviewed.effects_digest is not None
    replacement = profiles.load(
        profile.number,
        request_key=_uuid(18822),
        actor="admin",
        reviewed_effects_digest=reviewed.effects_digest,
    )
    restarted = build_production_fleet_profile_service(
        sessions, clock=lambda: now[0], run_switch_operations=switches
    )
    with sessions() as session:
        accepted = session.get(FleetProfileApplication, replacement.id)
        old = session.get(Job, old_start.id)
        assert accepted is not None and old is not None
        assert ("job", old_start.id, tuple(nodes)) in _superseded(
            _persisted_profile_plan(accepted)
        )
        assert old.request_id == _uuid(18821) and old.kind == "recipe.start"
        stored_run = session.get(RecipeRun, run_id)
        assert stored_run is not None and stored_run.state != "stopped"
    assert fenced_attempt(sessions, old_claim).result == original_evidence
    assert fenced_operation(sessions, old_claim).payload == original_payload
    assert _held_run_claims(sessions, run_id) == claims
    _child, stop_job = _start_profile_stop_child(
        sessions, switches, restarted, replacement
    )
    for index, node_id in enumerate(nodes):
        _, claim = _agent_service_and_target_claim(
            sessions,
            lifecycle,
            node_id,
            nodes,
            clock=lambda: now[0],
            jobs=jobs,
        )
        assert claim is not None and claim.operation.value == "recipe.stop"
        assert fenced_operation(sessions, claim).parent_job_id == stop_job
        stop = RecipeStopPayload.model_validate(claim.payload)
        assert stop.run_id == run_id and stop.target_runtime_id == run_id
        jobs.record_result(_agent_result(claim, state="succeeded", result={}))
        if index == 0:
            with sessions() as session:
                stored_run = session.get(RecipeRun, run_id)
                assert stored_run is not None and stored_run.state != "stopped"
            assert _held_run_claims(sessions, run_id)
    for _ in range(8):
        _follow_profile_retry(restarted, replacement.id, now)
        switches.tick()
        restarted.tick()
    assert restarted.application(replacement.id).state == LifecycleState.SUCCEEDED
    assert not _held_run_claims(sessions, run_id)
    with sessions() as session:
        ranks = list(session.scalars(select(RunNode).where(RunNode.run_id == run_id)))
        assert {rank.node_id for rank in ranks} == set(nodes)
        assert all(rank.state == "stopped" for rank in ranks)
        stored_run = session.get(RecipeRun, run_id)
        stored_start = session.get(Job, old_start.id)
        assert stored_run is not None and stored_start is not None
        assert stored_run.state == "stopped"
        assert stored_start.request_id == _uuid(18821)
    assert fenced_attempt(sessions, old_claim).result == original_evidence
