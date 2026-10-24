"""Reviewed profile decisions bind effects, independent of progress observations."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select
from vonk_agent_protocol import LifecycleState, ProfileReasonCode
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import (
    FleetProfileConflict,
    FleetProfileReviewStale,
    FleetProfileService,
    build_production_fleet_profile_service,
)
from vonk_control.models import (
    AgentNode,
    CatalogDocumentRevision,
    FleetProfileApplication,
    InstallationNode,
    NodeInventorySnapshot,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
    User,
)
from vonk_control.preparation_contract import RolloutPreparation
from vonk_control.run_switch_contract import RunSwitchAssessment

from cluster_profiles.cli_render import render_payload

from .non_blocking import assert_ended_without_blocking
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, inventory_items_fixture
from .test_fleet_profiles import (
    NOW,
    _assessment,
    _database,
    _exact_cleanup_profile,
    _exact_preparation,
    _input,
    _node_id,
    _seed,
    _SwitchAdapter,
)
from .test_recipe_operations import started_recipe


def test_profile_review_exposes_planner_headroom_without_hashing_free_memory(
    tmp_path, capsys
):
    from vonk_control.run_switch_operations import RunSwitchOperationService

    from .test_recipe_operations import setup_services
    from .test_run_switch_operations import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    planner = RunSwitchOperationService(
        sessions,
        memory_floor_bytes=0,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
    )
    service = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=planner
    )
    with sessions() as session:
        recipe = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
        assert recipe is not None
        selector = f"{recipe.publisher}/{recipe.slug}"
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Capacity review",
                "assignments": [
                    {"recipe_selector": selector, "spark_ids": list(nodes)}
                ],
            }
        ),
        actor="admin",
    )
    reviewed = service.preview(profile.id)
    assert reviewed.allowed
    fit = reviewed.assessments[0].assessment.fit_current.nodes[0]
    assert fit.memory_available_bytes is not None
    assert fit.memory_required_bytes is not None
    assert fit.memory_free_after_bytes == (
        fit.memory_available_bytes - fit.memory_required_bytes
    )
    render_payload(reviewed.model_dump(mode="json"), "profile", action="preview")
    rendered = capsys.readouterr().out
    assert "Memory after placement" in rendered
    assert "Memory demand kind: unified" in rendered
    assert "Physical memory pool: shared" in rendered
    assert "Memory reserve: 0 B" in rendered
    assert "Ports required" in rendered
    assert fit.ports_required and all(
        str(port) in rendered for port in fit.ports_required
    )

    with sessions.begin() as session:
        inventory = session.scalar(select(NodeInventorySnapshot))
        assert inventory is not None
        inventory.host_memory_free_bytes -= 1
        inventory.gpu_memory_free_bytes -= 1
    changed_observation = service.preview(profile.id)
    assert changed_observation.allowed
    assert changed_observation.assessments != reviewed.assessments
    assert changed_observation.plan_digest == reviewed.plan_digest

    # A policy change is part of the consent, unlike a fresh free-byte count.
    planner._memory_floor = 1
    changed_policy = service.preview(profile.id)
    assert changed_policy.allowed
    assert changed_policy.plan_digest != reviewed.plan_digest
    assert changed_policy.admission_decisions[0].requirements[0].memory_floor_bytes == 1

    with sessions.begin() as session:
        inventory = session.scalar(select(NodeInventorySnapshot))
        assert inventory is not None
        inventory.host_memory_free_bytes = 0
        inventory.gpu_memory_free_bytes = 0
        # Another Vonk workload holds the memory, so the shortage is a wait
        # (with nothing Vonk holds there it could never fit: the load ends).
        session.add(
            ResourceReservation(
                node_id=inventory.node_id,
                kind="unified-memory",
                resource_key="other-vonk-workload",
                amount_bytes=1,
                owner_kind="run",
                owner_id=str(uuid4()),
                state="active",
                plan_digest="d" * 64,
                created_at=inventory.observed_at,
            )
        )
    blocked = service.preview(profile.id)
    assert not blocked.allowed
    assert blocked.plan_digest != reviewed.plan_digest
    assert blocked.assessments[0].assessment.blockers
    dropped_reasons = blocked.assessments[0].assessment.model_dump(mode="json")
    dropped_reasons.update(allowed=True, blockers=[])
    with pytest.raises(ValueError):
        RunSwitchAssessment.model_validate_json(json.dumps(dropped_reasons))
    render_payload(blocked.model_dump(mode="json"), "profile", action="preview")
    assert "short by" in capsys.readouterr().out
    # The latest load request leads: an older review digest is advisory, and a
    # capacity shortage parks the accepted intent with a visible next attempt.
    parked = service.apply(
        profile.id,
        request_key=str(uuid4()),
        actor="admin",
    )
    assert parked.state == "queued"
    assert parked.progress.admission_pending is True
    assert "Next attempt" in (parked.status_reason or "")
    with sessions() as session:
        assert len(tuple(session.scalars(select(FleetProfileApplication)))) == 1


def _replace_run(sessions, run_id: str) -> str:
    replacement_id = str(uuid4())
    with sessions.begin() as session:
        original = session.get(RecipeRun, run_id)
        assert original is not None
        session.add(
            RecipeRun(
                **{
                    column.key: getattr(original, column.key)
                    for column in inspect(RecipeRun).column_attrs
                    if column.key != "id"
                },
                id=replacement_id,
            )
        )
        original.state = "stopped"
        original.route_state = "withdrawn"
        for node in session.scalars(
            select(RunNode).where(RunNode.run_id == original.id)
        ):
            session.add(
                RunNode(
                    **{
                        column.name: getattr(node, column.name)
                        for column in RunNode.__table__.columns
                        if column.name not in {"id", "run_id"}
                    },
                    id=str(uuid4()),
                    run_id=replacement_id,
                )
            )
    return replacement_id


def test_replacing_a_run_on_the_same_nodes_requires_another_review(tmp_path, capsys):
    sessions, lifecycle, _adapter, service, profile, installed, nodes = (
        _exact_cleanup_profile(tmp_path)
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installed.owner_id,
        nodes,
        request_id=str(uuid4()),
        alias="reviewed-endpoint",
    )
    reviewed = service.preview(profile.id)
    render_payload(reviewed.model_dump(mode="json"), "profile", action="preview")
    output = capsys.readouterr().out
    assert "Stop endpoint: reviewed-endpoint" in output
    assert run.owner_id in output
    assert all(node in output for node in nodes)
    _replace_run(sessions, run.owner_id)
    refreshed = service.preview(profile.id)
    assert reviewed.summary.stops == refreshed.summary.stops == 1
    assert reviewed.steps == refreshed.steps
    assert reviewed.plan_digest != refreshed.plan_digest
    # The load applies the current plan (stopping the replacement run), not
    # the stale review of the run it replaced.
    applied = service.apply(
        profile.id,
        request_key=str(uuid4()),
        actor="admin",
    )
    intended = applied.progress.intended_profile
    assert intended is not None
    assert intended.reviewed_plan_digest == refreshed.plan_digest


def test_review_ignores_transfer_counters_but_binds_reuse_and_blockers():
    sessions = _database()
    _recipe_id, revision = _seed(sessions)
    evidence = _exact_preparation((_node_id(1),)).model_dump(mode="json")
    target = evidence["model"]["targets"][0]
    target.update(
        state="preparing",
        present_bytes=10,
        missing_bytes=90,
        verified_sha256=None,
        verified_at=None,
    )
    evidence.update(targets_ready=False, ready=False)
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=lambda *_args, **_kwargs: _assessment(
            RolloutPreparation.model_validate_json(json.dumps(evidence))
        ),
    )
    profile = service.create(_input(revision), actor="admin")
    review = service.preview(profile.id)
    target.update(present_bytes=60, missing_bytes=40)
    later = service.preview(profile.id)
    assert later.plan_digest == review.plan_digest
    assert later.preparations != review.preparations
    target.update(
        state="ready",
        present_bytes=100,
        missing_bytes=0,
        verified_sha256="a" * 64,
        verified_at=NOW.isoformat(),
    )
    evidence.update(targets_ready=True, ready=True)
    reusable = service.preview(profile.id)
    assert reusable.plan_digest != review.plan_digest
    evidence["reasons"] = [
        {
            "code": "preparation.revoked",
            "detail": "Exact asset is no longer authorized",
            "severity": "blocker",
            "node_ids": [_node_id(1)],
        }
    ]
    evidence["ready"] = False
    refused = service.preview(profile.id)
    assert not refused.allowed
    assert refused.plan_digest != reusable.plan_digest


@pytest.mark.parametrize("new_effect", ["stop", "remove"])
def test_retry_cannot_expand_destructive_effects_outside_original_consent(
    tmp_path, new_effect
):
    sessions, lifecycle, _adapter, service, profile, installed, nodes = (
        _exact_cleanup_profile(tmp_path)
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installed.owner_id,
        nodes,
        request_id=str(uuid4()),
        alias="reviewed-endpoint",
    )
    application = service.apply(
        profile.id,
        request_key=str(uuid4()),
        actor="admin",
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.state = "backoff"
        row.status_reason = "Worker stopped before dispatch"
    if new_effect == "stop":
        replacement_id = _replace_run(sessions, run.owner_id)
    else:
        replacement_id = str(uuid4())
        with sessions.begin() as session:
            original = session.get(RecipeInstallation, installed.owner_id)
            assert original is not None
            session.add(
                RecipeInstallation(
                    **{
                        column.name: getattr(original, column.name)
                        for column in RecipeInstallation.__table__.columns
                        if column.name != "id"
                    },
                    id=replacement_id,
                )
            )
            for member in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installed.owner_id
                )
            ):
                session.add(
                    InstallationNode(
                        **{
                            column.name: getattr(member, column.name)
                            for column in InstallationNode.__table__.columns
                            if column.name not in {"id", "installation_id"}
                        },
                        id=str(uuid4()),
                        installation_id=replacement_id,
                    )
                )
    observed = service.retry(application.id, request_key=str(uuid4()), actor="admin")
    assert observed.id == application.id
    assert observed.next_attempt_at is not None
    with sessions() as session:
        assert len(tuple(session.scalars(select(FleetProfileApplication)))) == 1
        if new_effect == "stop":
            replacement = session.get(RecipeRun, replacement_id)
            assert replacement is not None and replacement.state == "running"
        else:
            installation = session.get(RecipeInstallation, replacement_id)
            assert installation is not None and installation.state == "installed"


def test_review_binds_idle_roster_and_definition_but_not_observation_time():
    sessions = _database()
    _seed(sessions)
    now = NOW
    service = FleetProfileService(sessions, clock=lambda: now)
    profile = service.create(FleetProfileInput(name="Idle review"), actor="admin")
    review = service.preview(profile.id)
    now += timedelta(minutes=1)
    later = service.preview(profile.id)
    assert later.generated_at != review.generated_at
    assert later.plan_digest == review.plan_digest
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=_node_id(2),
                state="active",
                protocol_version=2,
                last_seen_at=now,
                inventory_snapshot=inventory_items_fixture(
                    _node_id(2),
                    observed_at=now,
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
            )
        )
    expanded = service.preview(profile.id)
    assert expanded.scope.idle_node_ids == [_node_id(1), _node_id(2)]
    assert expanded.plan_digest != review.plan_digest
    service.update(
        profile.id,
        FleetProfileInput(
            name="Idle review",
            description="New reviewed intent",
            expected_revision=profile.revision,
        ),
        actor="admin",
    )
    assert service.preview(profile.id).plan_digest != expanded.plan_digest


def test_admitted_review_replay_ignores_later_edits_but_binds_issuer_and_digest():
    sessions = _database()
    _recipe_id, revision = _seed(sessions)
    with sessions.begin() as session:
        session.add(User(subject="another-admin", role="administrator"))
    adapter = _SwitchAdapter()
    service = FleetProfileService(sessions, clock=lambda: NOW, switch_adapter=adapter)
    profile = service.create(_input(revision), actor="admin")
    review = service.preview(profile.id)
    key = str(uuid4())
    accepted = service.apply(profile.id, actor="admin", request_key=key)
    intended = accepted.progress.intended_profile
    assert intended is not None
    assert intended.reviewed_plan_digest == review.plan_digest
    assert accepted.plan_digest != intended.reviewed_plan_digest
    service.update(
        profile.id,
        FleetProfileInput(name="Changed to idle", expected_revision=profile.revision),
        actor="admin",
    )
    assert service.apply(profile.id, actor="admin", request_key=key) == accepted
    # The issuer is bound to the request key; the review digest is advisory,
    # so a replay under the same key returns the accepted application.
    with pytest.raises(FleetProfileConflict):
        service.apply(
            profile.id,
            actor="another-admin",
            request_key=key,
        )
    assert service.apply(profile.id, actor="admin", request_key=key) == accepted
    with sessions() as session:
        assert list(session.scalars(select(FleetProfileApplication.id))) == [
            accepted.id
        ]
    assert not adapter.starts


@pytest.mark.parametrize("remove_review_source", [False, True])
def test_retry_checks_original_review_while_reusing_newly_ready_assets(
    remove_review_source,
):
    sessions = _database()
    _recipe_id, revision = _seed(sessions)
    evidence = _exact_preparation((_node_id(1),)).model_dump(mode="json")
    evidence["model"]["targets"][0].update(
        state="preparing",
        present_bytes=0,
        missing_bytes=100,
        verified_sha256=None,
        verified_at=None,
    )
    evidence.update(targets_ready=False, ready=False)
    adapter = _SwitchAdapter()
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=adapter,
        assessment_provider=lambda *_args, **_kwargs: _assessment(
            RolloutPreparation.model_validate_json(json.dumps(evidence))
        ),
    )
    profile = service.create(_input(revision), actor="admin")
    original_review = service.preview(profile.id)
    original = service.apply(
        profile.id,
        request_key=str(uuid4()),
        actor="admin",
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, original.id)
        assert row is not None
        row.state = "backoff"
        row.status_reason = "Worker stopped before dispatch"
    # Completed transfer changes remaining work, without changing any desired
    # assignment or authorizing another model, image, endpoint, or Spark.
    evidence = _exact_preparation((_node_id(1),)).model_dump(mode="json")
    assert service.preview(profile.id).plan_digest != original_review.plan_digest
    retried = service.retry(original.id, request_key=str(uuid4()), actor="admin")
    assert retried.progress.intended_profile is not None
    assert retried.progress.intended_profile.reviewed_application_id == original.id
    assert (
        retried.progress.intended_profile.reviewed_plan_digest
        == original_review.plan_digest
    )
    if remove_review_source:
        with sessions.begin() as session:
            root = session.get(FleetProfileApplication, original.id)
            assert root is not None
            session.delete(root)
    assert service.tick()
    observed = service.application(retried.id)
    if remove_review_source:
        assert observed.state == LifecycleState.QUEUED
        assert observed.next_attempt_at is not None
        # Missing historical evidence cannot authorize replacement effects.
        # Nothing live works for it: a whole window later the attempt ends
        # with that stall evidence (never as an operator cancel).
        now = NOW + timedelta(hours=2)
        service._clock = lambda: now
        for _ in range(32):
            service.tick()
            now += timedelta(seconds=30)
        with sessions() as session:
            row = session.get(FleetProfileApplication, retried.id)
            assert row is not None
            ended = SimpleNamespace(
                state=row.state,
                status_reason=row.status_reason,
                request_key=row.request_key,
            )

        def stall_evidence(receipt):
            # Ended failed with its cause; its automatic recovery is parked.
            assert "unavailable" in (receipt.status_reason or "")

        assert_ended_without_blocking(
            SimpleNamespace(sessions=sessions),
            ended,
            end=lambda receipt: receipt,
            fresh=lambda _world: service.apply(
                profile.id, request_key=str(uuid4()), actor="admin"
            ),
            assert_reason=stall_evidence,
        )
        fresh = service.apply(profile.id, request_key=str(uuid4()), actor="admin")
        assert adapter.starts == []
        assert fresh.id not in {original.id, retried.id}
    else:
        assert observed.state == "running", observed.status_reason
        assert len(adapter.starts) == 1


def _stale_refusal_leaves_nothing(sessions) -> None:
    with sessions() as session:
        assert not tuple(session.scalars(select(FleetProfileApplication)))


@pytest.mark.parametrize("change", ["replaced-workload", "edited-profile"])
def test_load_bound_to_a_review_is_refused_when_the_effects_changed(tmp_path, change):
    sessions, lifecycle, _adapter, service, profile, installed, nodes = (
        _exact_cleanup_profile(tmp_path)
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installed.owner_id,
        nodes,
        request_id=str(uuid4()),
        alias="reviewed-endpoint",
    )
    reviewed = service.preview(profile.id)
    assert reviewed.effects_digest is not None
    if change == "replaced-workload":
        _replace_run(sessions, run.owner_id)
    else:
        service.update(
            profile.id,
            FleetProfileInput.model_validate(
                {
                    "name": "Exact cleanup",
                    "installation_policy": "keep-cached",
                    "assignments": [],
                    "expected_revision": profile.revision,
                }
            ),
            actor="admin",
        )
    key = str(uuid4())

    with pytest.raises(FleetProfileReviewStale):
        service.apply(
            profile.id,
            request_key=key,
            actor="admin",
            reviewed_effects_digest=reviewed.effects_digest,
        )

    _stale_refusal_leaves_nothing(sessions)
    # Nothing was accepted, so the current plan can be reviewed and loaded.
    current = service.preview(profile.id)
    assert current.effects_digest != reviewed.effects_digest
    accepted = service.apply(
        profile.id,
        request_key=key,
        actor="admin",
        reviewed_effects_digest=current.effects_digest,
    )
    assert accepted.request_key == key


def test_load_bound_to_a_review_is_accepted_when_only_observations_changed(tmp_path):
    sessions, lifecycle, _adapter, service, profile, installed, nodes = (
        _exact_cleanup_profile(tmp_path)
    )
    started_recipe(
        sessions,
        lifecycle,
        installed.owner_id,
        nodes,
        request_id=str(uuid4()),
        alias="reviewed-endpoint",
    )
    reviewed = service.preview(profile.id)
    with sessions.begin() as session:
        inventory = session.scalar(select(NodeInventorySnapshot))
        assert inventory is not None
        inventory.host_memory_free_bytes -= 1
        inventory.gpu_memory_free_bytes -= 1
    service._clock = lambda: NOW + timedelta(minutes=5)

    refreshed = service.preview(profile.id)

    assert refreshed.generated_at != reviewed.generated_at
    assert refreshed.effects_digest == reviewed.effects_digest
    accepted = service.apply(
        profile.id,
        request_key=str(uuid4()),
        actor="admin",
        reviewed_effects_digest=reviewed.effects_digest,
    )
    assert accepted.state in {"queued", "running", "succeeded"}


def test_review_binds_effects_but_not_readiness_counters_or_blockers():
    sessions = _database()
    _recipe_id, revision = _seed(sessions)
    evidence = _exact_preparation((_node_id(1),)).model_dump(mode="json")
    target = evidence["model"]["targets"][0]
    target.update(
        state="preparing",
        present_bytes=10,
        missing_bytes=90,
        verified_sha256=None,
        verified_at=None,
    )
    evidence.update(targets_ready=False, ready=False)
    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SwitchAdapter(),
        assessment_provider=lambda *_args, **_kwargs: _assessment(
            RolloutPreparation.model_validate_json(json.dumps(evidence))
        ),
    )
    profile = service.create(_input(revision), actor="admin")
    review = service.preview(profile.id)
    target.update(
        state="ready",
        present_bytes=100,
        missing_bytes=0,
        verified_sha256="a" * 64,
        verified_at=NOW.isoformat(),
    )
    evidence.update(targets_ready=True, ready=True)
    reusable = service.preview(profile.id)
    evidence["reasons"] = [
        {
            "code": "preparation.revoked",
            "detail": "Exact asset is no longer authorized",
            "severity": "blocker",
            "node_ids": [_node_id(1)],
        }
    ]
    evidence["ready"] = False
    refused = service.preview(profile.id)

    # The plan digest follows readiness and blockers; the reviewed effects do not.
    assert len({review.plan_digest, reusable.plan_digest, refused.plan_digest}) == 3
    assert review.effects_digest == reusable.effects_digest == refused.effects_digest


@pytest.mark.parametrize("initially_waiting", [False, True])
def test_resource_recheck_contract_error_replans_then_admits(
    tmp_path, postgres_engine, monkeypatch, initially_waiting: bool
) -> None:
    """Catch a stale recheck ending a valid load instead of freshly assessing it."""
    from vonk_control.run_switch_operations import RunSwitchOperationService

    from .test_recipe_operations import setup_services
    from .test_run_switch_operations import (
        CompleteArtifactInspector,
        RecordingArtifactExecutor,
    )

    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    planner = RunSwitchOperationService(
        sessions,
        memory_floor_bytes=0,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
    )
    clock = [NOW]

    def service():
        return build_production_fleet_profile_service(
            sessions, clock=lambda: clock[0], run_switch_operations=planner
        )

    owner = service()
    with sessions() as session:
        recipe = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
        assert recipe is not None
        selector = f"{recipe.publisher}/{recipe.slug}"
    profile = owner.create(
        FleetProfileInput.model_validate(
            {
                "name": "Recheck defect",
                "assignments": [
                    {"recipe_selector": selector, "spark_ids": list(nodes)}
                ],
            }
        ),
        actor="admin",
    )
    reviewed = owner.preview(profile.id)
    assert reviewed.allowed
    recheck = planner.recheck_resources_in_session
    transient = initially_waiting
    inject_defect = True
    calls = 0

    def injected_recheck(*args, **kwargs):
        nonlocal calls
        calls += 1
        if transient:
            raise ValueError("resource observation temporarily unavailable")
        assessment = recheck(*args, **kwargs)
        if inject_defect:
            return RunSwitchAssessment.model_validate(
                {**assessment.model_dump(), "allowed": False}
            )
        return assessment

    monkeypatch.setattr(planner, "recheck_resources_in_session", injected_recheck)
    application = owner.apply(profile.id, request_key=str(uuid4()), actor="admin")
    if initially_waiting:
        assert application.state == LifecycleState.QUEUED
        assert application.next_attempt_at is not None
        transient = False
        clock[0] += timedelta(minutes=1)
        owner = service()
        assert owner.tick()
        application = owner.application(application.id)

    assert application.state == LifecycleState.QUEUED
    assert application.progress.admission_pending
    assert any(
        reason.code == ProfileReasonCode.STALE_PLAN
        for reason in application.progress.blockers
    )
    observed_calls = calls
    inject_defect = False
    restarted = service()
    assessments = []
    assess = restarted._assessment_provider
    assert assess is not None

    def fresh_assessment(*args, **kwargs):
        result = assess(*args, **kwargs)
        assessments.append(result)
        return result

    restarted._assessment_provider = fresh_assessment
    # The fresh assessment may have the same digest: the stored-plan error is
    # not proof of a defect in a fresh plan. Re-plan, then admit next tick.
    for _ in range(3):
        assert restarted.tick()
        application = restarted.application(application.id)
        if not application.progress.admission_pending:
            break
    assert not application.progress.admission_pending
    assert application.state != LifecycleState.FAILED
    assert calls > observed_calls
    assert len(assessments) == 1
    assert not any(
        reason.code == ProfileReasonCode.ADMISSION_DEFECT
        for reason in application.progress.blockers
    )


def test_deferred_target_builder_review_refreshes_inventory_before_admission(
    tmp_path, postgres_engine
) -> None:
    """Catch t0 builder freshness overriding t1 residual evidence on resume."""
    from vonk_agent_protocol.agent_words import ProfileOperationKind
    from vonk_control.models import FleetProfile
    from vonk_control.recipe_execution_contract import parse_stored_installation_plan

    from .test_profile_build_memory import _accepted_build_profile

    sessions, profiles, planner, node_id, _application, build = _accepted_build_profile(
        tmp_path, postgres_engine, accept_profile=False
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    with sessions.begin() as session:
        profile = session.scalar(select(FleetProfile))
        installation = session.scalar(select(RecipeInstallation))
        sample = session.scalar(select(NodeInventorySnapshot))
        assert profile is not None and installation is not None and sample is not None
        profile_id, installation_id = profile.id, installation.id
        installed = parse_stored_installation_plan(installation.plan)
        image = next(iter(installed.compiled_execution_plans.values())).runtime_image
        sample.host_memory_free_bytes = sample.gpu_memory_free_bytes = 8_000
    run_plan = lifecycle._run_admission.plan_run(
        installation_id, "old", now=profiles._clock()
    )
    run_id = lifecycle._run_admission.accept_run(
        run_plan, actor="admin", now=profiles._clock()
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = LifecycleState.RUNNING
        for rank in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            rank.state = LifecycleState.RUNNING
    # Cache-repair review binds the already accepted exact image while its
    # source build is pending, just as a recovering application does.
    assess = profiles._assessment_provider
    assert assess is not None

    def recovering_review(session, assignment, expected_nodes, **kwargs):
        kwargs["expected_runtime_image"] = image
        return assess(session, assignment, expected_nodes, **kwargs)

    profiles._assessment_provider = recovering_review
    reviewed = profiles.preview(profile_id, allow_pending_cache_rebuild=True)
    assert reviewed.allowed, [item.assessment.blockers for item in reviewed.assessments]
    assessment = reviewed.assessments[0].assessment
    uncertainty = assessment.fit_current.nodes[0].memory_usage_uncertainty
    assert uncertainty is not None
    assert assessment.preparation is not None
    assert build.builder_node_id == node_id
    pending = profiles._create_pending_application(
        reviewed,
        request_key=str(uuid4()),
        actor="admin",
        operation_kind=ProfileOperationKind.APPLY.value,
        select_profile=True,
    )
    profiles._defer_pending_application(
        pending.id, "Admission lock contention", retry_delay=timedelta(0)
    )
    with sessions.begin() as session:
        sample = session.scalar(select(NodeInventorySnapshot))
        assert sample is not None
        values = {
            attribute.key: getattr(sample, attribute.key)
            for attribute in NodeInventorySnapshot.__mapper__.column_attrs
            if attribute.key != "id"
        }
        values.update(
            id=str(uuid4()),
            observed_at=sample.observed_at + timedelta(seconds=1),
            evidence_digest="b" * 64,
        )
        session.add(NodeInventorySnapshot(**values))
    checked = []
    recheck = planner.recheck_resources_in_session

    def observe_recheck(*args, **kwargs):
        result = recheck(*args, **kwargs)
        checked.append(result)
        return result

    planner.recheck_resources_in_session = observe_recheck
    assert profiles._observe_pending_admissions(profiles._clock())
    admitted = profiles.application(pending.id)
    assert not admitted.progress.admission_pending
    assert admitted.state != LifecycleState.FAILED
    assert len(checked) == 1
    fresh_uncertainty = checked[0].fit_current.nodes[0].memory_usage_uncertainty
    assert fresh_uncertainty is not None
    assert fresh_uncertainty.inventory_evidence_digest == "b" * 64
