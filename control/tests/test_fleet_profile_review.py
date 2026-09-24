"""Reviewed profile decisions bind effects, independent of progress observations."""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import (
    FleetProfileConflict,
    FleetProfileService,
    FleetProfileStalePlanConflict,
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
    RunNode,
    User,
)
from vonk_control.preparation_contract import RolloutPreparation
from vonk_control.run_switch_contract import RunSwitchAssessment

from cluster_profiles.cli_render import render_payload

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
    blocked = service.preview(profile.id)
    assert not blocked.allowed
    assert blocked.plan_digest != reviewed.plan_digest
    assert blocked.assessments[0].assessment.blockers
    dropped_reasons = blocked.assessments[0].assessment.model_dump(mode="json")
    dropped_reasons.update(allowed=True, blockers=[])
    with pytest.raises(ValueError, match="preparation blockers must be named"):
        RunSwitchAssessment.model_validate_json(json.dumps(dropped_reasons))
    render_payload(blocked.model_dump(mode="json"), "profile", action="preview")
    assert "short by" in capsys.readouterr().out
    with pytest.raises(FleetProfileStalePlanConflict):
        service.apply(
            profile.id,
            plan_digest=reviewed.plan_digest,
            request_key=str(uuid4()),
            actor="admin",
        )
    with pytest.raises(FleetProfileConflict, match="blocked"):
        service.apply(
            profile.id,
            plan_digest=blocked.plan_digest,
            request_key=str(uuid4()),
            actor="admin",
        )
    with sessions() as session:
        assert session.scalar(select(FleetProfileApplication)) is None


def _replace_run(sessions, run_id: str) -> str:
    replacement_id = str(uuid4())
    with sessions.begin() as session:
        original = session.get(RecipeRun, run_id)
        assert original is not None
        session.add(
            RecipeRun(
                **{
                    column.name: getattr(original, column.name)
                    for column in RecipeRun.__table__.columns
                    if column.name != "id"
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
    with pytest.raises(FleetProfileConflict, match="stale"):
        service.apply(
            profile.id,
            plan_digest=reviewed.plan_digest,
            request_key=str(uuid4()),
            actor="admin",
        )
    with sessions() as session:
        assert session.scalar(select(FleetProfileApplication)) is None


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
    with pytest.raises(FleetProfileConflict, match="stale"):
        service.apply(
            profile.id,
            plan_digest=review.plan_digest,
            actor="admin",
            request_key=str(uuid4()),
        )
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
    assert refused.preparation_decisions[0].blockers[0].code == "preparation.revoked"


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
    reviewed = service.preview(profile.id)
    application = service.apply(
        profile.id,
        plan_digest=reviewed.plan_digest,
        request_key=str(uuid4()),
        actor="admin",
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.state = "failed"
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
    with pytest.raises(FleetProfileConflict, match="unreviewed"):
        service.retry(application.id, request_key=str(uuid4()), actor="admin")
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
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=now,
            )
        )
    expanded = service.preview(profile.id)
    assert expanded.scope.idle_node_ids == [_node_id(1), _node_id(2)]
    assert expanded.plan_digest != review.plan_digest
    with pytest.raises(FleetProfileConflict, match="stale"):
        service.apply(
            profile.id,
            plan_digest=review.plan_digest,
            actor="admin",
            request_key=str(uuid4()),
        )
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
    accepted = service.apply(
        profile.id, plan_digest=review.plan_digest, actor="admin", request_key=key
    )
    intended = accepted.progress.intended_profile
    assert intended is not None
    assert intended.reviewed_plan_digest == review.plan_digest
    assert accepted.plan_digest != intended.reviewed_plan_digest
    service.update(
        profile.id,
        FleetProfileInput(name="Changed to idle", expected_revision=profile.revision),
        actor="admin",
    )
    assert (
        service.apply(
            profile.id, plan_digest=review.plan_digest, actor="admin", request_key=key
        )
        == accepted
    )
    for actor, digest in (("another-admin", review.plan_digest), ("admin", "f" * 64)):
        with pytest.raises(FleetProfileConflict, match="request key"):
            service.apply(profile.id, plan_digest=digest, actor=actor, request_key=key)
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
        plan_digest=original_review.plan_digest,
        request_key=str(uuid4()),
        actor="admin",
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, original.id)
        assert row is not None
        row.state = "failed"
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
        assert observed.state == "failed"
        assert "review source" in (observed.status_reason or "")
        assert adapter.starts == []
    else:
        assert observed.state == "running", observed.status_reason
        assert len(adapter.starts) == 1
