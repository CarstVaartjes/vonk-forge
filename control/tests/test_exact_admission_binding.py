"""Admission must re-observe facts without silently replacing reviewed effects."""

from dataclasses import replace

import pytest
from sqlalchemy import select
from vonk_agent_protocol import UnknownOutcomeError, WaitReason
from vonk_control.install_admission import InstallPlanStale
from vonk_control.models import Job, RecipeInstallation, RecipeRun
from vonk_control.run_admission import RunPlanInvalid

from .test_recipe_operations import installed_recipe, setup_services


@pytest.mark.parametrize("method", ["install", "start", "activate_job_run"])
def test_precommit_retry_refuses_changed_execution_effects(
    method, tmp_path, monkeypatch
):
    """Catch a retry that quietly substitutes another image or endpoint port."""
    from .test_artifact_jobs import _configure_artifact_recipe

    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        recipe_transform=_configure_artifact_recipe
        if method == "activate_job_run"
        else None,
    )
    if method == "install":
        plan = service.preview_install(mapping_id, build_id)
        admission, planner, error = (
            service._install_admission,
            "plan_install",
            InstallPlanStale,
        )
    else:
        installed = installed_recipe(
            service, mapping_id, build_id, nodes, request_id="1" * 36
        )
        plan = service.preview_run(
            installed.owner_id, "image-job" if method == "activate_job_run" else "qwen"
        )
        admission, planner, error = service._run_admission, "plan_run", RunPlanInvalid
    original = getattr(admission, planner)
    calls = []

    def changed(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise UnknownOutcomeError(
                "lost precommit evidence", reason=WaitReason.OBSERVATION_UNAVAILABLE
            )
        current = original(*args, **kwargs)
        return (
            replace(current, image_digest="sha256:" + "d" * 64)
            if method == "install"
            else replace(
                current,
                nodes=(
                    replace(current.nodes[0], port=current.nodes[0].port + 1),
                    *current.nodes[1:],
                ),
            )
        )

    monkeypatch.setattr(admission, planner, changed)
    with pytest.raises(error):
        getattr(service, method)(
            plan, plan_digest=plan.plan_digest, actor="operator", request_id="2" * 36
        )
    assert len(calls) == 2
    with sessions() as session:
        assert session.scalar(select(Job).where(Job.request_id == "2" * 36)) is None
        assert list(session.scalars(select(RecipeRun))) == []
        if method == "install":
            assert list(session.scalars(select(RecipeInstallation))) == []


@pytest.mark.parametrize("kind", ["install", "run"])
@pytest.mark.parametrize("changed_at", [1, 2])
def test_transactional_admission_cannot_replace_effects(
    kind, changed_at, tmp_path, monkeypatch
):
    """Catch both the first refresh and refresh after acquiring admission locks."""
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    if kind == "install":
        plan = service.preview_install(mapping_id, build_id)
        admission, planner, accept, error = (
            service._install_admission,
            "plan_install",
            "accept_install_in_session",
            InstallPlanStale,
        )
    else:
        installed = installed_recipe(
            service, mapping_id, build_id, nodes, request_id="1" * 36
        )
        plan = service.preview_run(installed.owner_id, "qwen")
        admission, planner, accept, error = (
            service._run_admission,
            "plan_run",
            "accept_run_in_session",
            RunPlanInvalid,
        )
    original = getattr(admission, planner)
    calls = []

    def changed(*args, **kwargs):
        calls.append(True)
        current = original(*args, **kwargs)
        if len(calls) != changed_at:
            return current
        return (
            replace(current, image_digest="sha256:" + "d" * 64)
            if kind == "install"
            else replace(
                current,
                nodes=(
                    replace(current.nodes[0], port=current.nodes[0].port + 1),
                    *current.nodes[1:],
                ),
            )
        )

    monkeypatch.setattr(admission, planner, changed)
    with pytest.raises(error), sessions.begin() as session:
        getattr(admission, accept)(
            session, plan, actor="operator", now=service._clock()
        )
    assert len(calls) == changed_at
    with sessions() as session:
        assert list(session.scalars(select(RecipeRun))) == []
        if kind == "install":
            assert list(session.scalars(select(RecipeInstallation))) == []


@pytest.mark.parametrize("kind", ["install", "run"])
def test_admission_reobserves_capacity_without_changing_effects(
    kind, tmp_path, monkeypatch
):
    """Catch an overbroad exact-plan check that mistakes fresh evidence for effects."""
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    if kind == "install":
        plan = service.preview_install(mapping_id, build_id)
        admission, planner, accept = (
            service._install_admission,
            "plan_install",
            "accept_install_in_session",
        )
    else:
        installed = installed_recipe(
            service, mapping_id, build_id, nodes, request_id="1" * 36
        )
        plan = service.preview_run(installed.owner_id, "qwen")
        admission, planner, accept = (
            service._run_admission,
            "plan_run",
            "accept_run_in_session",
        )
    original = getattr(admission, planner)

    def refreshed(*args, **kwargs):
        current = original(*args, **kwargs)
        updated = (
            replace(current.nodes[0], free_bytes=(current.nodes[0].free_bytes or 0) + 1)
            if kind == "install"
            else replace(
                current.nodes[0],
                available_memory_bytes=(current.nodes[0].available_memory_bytes or 0)
                + 1,
            )
        )
        return replace(current, nodes=(updated, *current.nodes[1:]))

    monkeypatch.setattr(admission, planner, refreshed)
    with sessions.begin() as session:
        owner = getattr(admission, accept)(
            session, plan, actor="operator", now=service._clock()
        )
    with sessions() as session:
        row = session.get(RecipeInstallation if kind == "install" else RecipeRun, owner)
        assert row is not None
        assert row.mapping_id == plan.mapping_id
