"""Admission races retain the accepted request and recover without a new load."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState, ProfileReasonCode
from vonk_control.fleet_profile_contract import FleetProfileApplicationView
from vonk_control.fleet_profiles import (
    FleetProfileService,
    FleetProfileStalePlanConflict,
)
from vonk_control.models import (
    AgentNode,
    ClusterMapping,
    FleetProfileApplication,
    RecipeBuild,
    RecipeRun,
)

from .test_fleet_profile_review import _replace_run
from .test_fleet_profiles import _exact_cleanup_profile
from .test_profile_capacity_admission import _capacity_profile
from .test_recipe_operations import installed_recipe, started_recipe


def test_direct_load_replans_after_installation_finishes_during_admission(
    tmp_path, postgres_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches apply deleting its receipt and returning 409 for changed effects.

    Finish an installation after review but before the real SQL admission
    recheck. The original load must re-plan to keep it, then be admitted under
    the same request identity, without another HTTP submission.
    """
    sessions, _, planner, profile, api, headers, _, nodes = _capacity_profile(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    with sessions() as session:
        mapping_id = session.scalar(select(ClusterMapping.id))
        build_id = session.scalar(select(RecipeBuild.id))
        assert mapping_id is not None and build_id is not None
    original_queue = FleetProfileService._queue_application
    injected = False

    def finish_installation(service, preview, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            installed_recipe(
                lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4())
            )
        return original_queue(service, preview, **kwargs)

    monkeypatch.setattr(FleetProfileService, "_queue_application", finish_installation)
    key = str(uuid4())
    response = api.post(
        f"/api/profile/{profile.number}/load",
        headers=headers,
        json={"request_key": key},
    )
    assert response.status_code == 202, response.text
    accepted = FleetProfileApplicationView.model_validate_json(response.content)
    assert accepted.progress.admission_pending
    assert any(b.code == ProfileReasonCode.STALE_PLAN for b in accepted.blockers)
    initial_digest = accepted.plan_digest

    # Reconstruct the service: the durable worker, not the submitting process,
    # owns the retry and the re-plan.
    from .test_profile_installed_execution import _profile_service

    recovered, _ = _profile_service(sessions, lifecycle)
    for _ in range(4):
        waiting = recovered.application(accepted.id)
        if not waiting.progress.admission_pending:
            break
        due = waiting.progress.admission_retry_at
        assert due is not None
        recovered._clock = lambda due=due: due
        assert recovered.tick()
    admitted = recovered.application(accepted.id)
    assert not admitted.progress.admission_pending, admitted
    assert admitted.state == LifecycleState.RUNNING
    assert admitted.plan_digest != initial_digest
    assert admitted.progress.switch_adapter is not None
    assert not admitted.blockers
    replay = api.post(
        f"/api/profile/{profile.number}/load",
        headers=headers,
        json={"request_key": key},
    )
    assert replay.status_code == 202, replay.text
    assert (
        FleetProfileApplicationView.model_validate_json(replay.content).id
        == accepted.id
    )
    with sessions() as session:
        assert list(session.scalars(select(FleetProfileApplication.id))) == [
            accepted.id
        ]


def test_direct_stale_refusal_that_replanning_cannot_change_ends_with_evidence(
    tmp_path, postgres_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches retrying an identical refused plan forever after the direct race."""
    _, profiles, _, profile, _, _, _, _ = _capacity_profile(tmp_path, postgres_engine)

    def refuse(*_args, **_kwargs):
        raise FleetProfileStalePlanConflict("injected unchanged admission assumption")

    monkeypatch.setattr(FleetProfileService, "_queue_application", refuse)
    accepted = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")
    due = accepted.progress.admission_retry_at
    assert due is not None
    profiles._clock = lambda: due
    assert profiles.tick()
    ended = profiles.application(accepted.id)
    assert ended.state == LifecycleState.FAILED
    assert not ended.progress.admission_pending
    (refusal,) = ended.blockers
    assert refusal.code == ProfileReasonCode.STALE_PLAN
    assert "injected unchanged admission assumption" in refusal.detail


@pytest.mark.parametrize("change", ["run", "roster"])
def test_pending_replan_cannot_expand_reviewed_consent(
    tmp_path, postgres_engine, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Catches replacing a review with new destructive targets or fleet membership."""
    sessions, lifecycle, _, profiles, profile, installed, nodes = (
        _exact_cleanup_profile(tmp_path, engine=postgres_engine)
    )
    original_run = started_recipe(
        sessions,
        lifecycle,
        installed.owner_id,
        nodes,
        request_id=str(uuid4()),
        alias="reviewed-stop",
    )
    with monkeypatch.context() as race:

        def refuse(*_args, **_kwargs):
            raise FleetProfileStalePlanConflict("injected admission race")

        race.setattr(FleetProfileService, "_queue_application", refuse)
        accepted = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")
    if change == "run":
        remaining_run_id = _replace_run(sessions, original_run.owner_id)
        cause = "unreviewed workload"
    else:
        remaining_run_id = original_run.owner_id
        cause = "fleet scope changed"
        with sessions.begin() as session:
            node = session.get(AgentNode, nodes[0])
            assert node is not None
            node.revoked_at = profiles._clock()
    for _ in range(4):
        waiting = profiles.application(accepted.id)
        if not waiting.progress.admission_pending:
            break
        due = waiting.progress.admission_retry_at
        assert due is not None
        profiles._clock = lambda due=due: due
        assert profiles.tick()
    ended = profiles.application(accepted.id)
    assert ended.state == LifecycleState.SUPERSEDED
    assert cause in (ended.status_reason or "")
    with sessions() as session:
        replacement = session.get(RecipeRun, remaining_run_id)
        assert replacement is not None and replacement.state == LifecycleState.RUNNING
