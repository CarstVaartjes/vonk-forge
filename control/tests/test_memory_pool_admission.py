"""Run, profile and builder admission protect the same physical memory."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.models import (
    ClusterMapping,
    NodeInventorySnapshot,
    RecipeBuild,
    ResourceReservation,
)
from vonk_control.recipe_builds import RecipeBuildError, RecipeBuildService
from vonk_control.recipe_execution_contract import parse_stored_build_plan
from vonk_control.recipe_operations import (
    RecipeOperationConflict,
    RecipeOperationService,
)
from vonk_control.resource_planning import (
    MemoryReservationTotals,
    PlannedStopRelease,
    ResourceDemand,
    memory_capacity_snapshot,
    plan_capacity,
)
from vonk_control.run_admission import RunAdmissionService, RunPlanConflict

from .test_profile_capacity_admission import _capacity_profile
from .test_recipe_builds import RecordingQueue
from .test_recipe_builds import setup as build_setup
from .test_recipe_operations import installed_recipe
from .test_run_admission import setup as run_setup


def _claim(sessions, node, now, *, kind, amount, owner):
    with sessions.begin() as session:
        session.add(
            ResourceReservation(
                node_id=node,
                kind=kind,
                resource_key="competing-memory",
                amount_bytes=amount,
                owner_kind=owner,
                owner_id=str(uuid4()),
                state="active",
                plan_digest="f" * 64,
                created_at=now,
            )
        )


@pytest.mark.parametrize("headroom", [0, -1])
def test_host_builder_claim_is_visible_to_unified_review_and_runtime(
    tmp_path, postgres_engine, headroom
):
    sessions, profiles, planner, profile, api, headers, _, nodes = _capacity_profile(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    with sessions() as session:
        mapping = session.scalar(select(ClusterMapping.id))
        build = session.scalar(select(RecipeBuild.id))
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert mapping is not None and build is not None and snapshot is not None
        free = min(snapshot.host_memory_free_bytes, snapshot.gpu_memory_free_bytes)
    installed = installed_recipe(
        lifecycle, mapping, build, nodes, request_id=str(uuid4())
    )
    original = lifecycle.preview_run(installed.owner_id, "memory-pool")
    required = original.nodes[0].required_memory_bytes
    planner._memory_floor = original.nodes[0].memory_floor_bytes
    _claim(
        sessions,
        nodes[0],
        profiles._clock(),
        kind="host-memory",
        amount=free - required - original.nodes[0].memory_floor_bytes - headroom,
        owner="recipe-build",
    )
    runtime = lifecycle.preview_run(installed.owner_id, "memory-pool")
    review = api.post(f"/api/profile/{profile.number}/preview", headers=headers)
    assert review.status_code == 200, review.text
    assert review.json()["allowed"] == runtime.allowed == (headroom == 0)
    node = review.json()["assessments"][0]["assessment"]["fit_current"]["nodes"][0]
    assert node["memory_free_after_bytes"] == runtime.nodes[0].free_after_bytes
    if headroom < 0:
        with pytest.raises(RecipeOperationConflict, match="stale_or_blocked"):
            lifecycle.start(
                original,
                plan_digest=original.plan_digest,
                actor="admin",
                request_id=str(uuid4()),
            )
    else:
        lifecycle.start(
            runtime,
            plan_digest=runtime.plan_digest,
            actor="admin",
            request_id=str(uuid4()),
        )


@pytest.mark.parametrize("pool", ["shared", "separate"])
def test_host_claim_competes_with_accelerator_only_when_pool_is_shared(
    tmp_path, postgres_engine, pool
):
    sessions, now, node, installation = run_setup(
        tmp_path, engine=postgres_engine, memory_kind="accelerator", memory_pool=pool
    )
    runs = RunAdmissionService(sessions)
    original = runs.plan_run(installation, "gpu", now=now)
    assert original.allowed
    _claim(sessions, node, now, kind="host-memory", amount=300, owner="recipe-build")
    current = runs.plan_run(installation, "gpu", now=now)
    assert current.allowed == (pool == "separate")
    if pool == "shared":
        with pytest.raises(RunPlanConflict):
            runs.accept_run(original, actor="admin", now=now)
    else:
        runs.accept_run(current, actor="admin", now=now)


@pytest.mark.parametrize(
    "pool,kind",
    [
        ("shared", "unified-memory"),
        ("shared", "gpu-memory"),
        ("separate", "gpu-memory"),
    ],
)
@pytest.mark.parametrize("headroom", [0, -1])
def test_builder_plan_and_acceptance_account_for_runtime_physical_pool(
    tmp_path, postgres_engine, pool, kind, headroom
):
    run_sessions, now, node, installation = run_setup(
        tmp_path,
        engine=postgres_engine,
        free_memory=1_000,
        memory_kind="unified" if kind == "unified-memory" else "accelerator",
        memory_pool=pool,
        system_reserve=50,
    )
    runs = RunAdmissionService(run_sessions)
    runtime = runs.plan_run(installation, "existing-runtime", now=now)
    assert runtime.allowed
    runs.accept_run(runtime, actor="admin", now=now)
    sessions, bundles, now, node, revision = build_setup(
        tmp_path, engine=postgres_engine, existing_node=True
    )
    with sessions.begin() as session:
        snapshot = session.scalar(
            select(NodeInventorySnapshot).order_by(
                NodeInventorySnapshot.observed_at.desc()
            )
        )
        assert snapshot is not None
        snapshot.memory_pool = pool
    builds = RecipeBuildService(sessions, bundles=bundles)
    original = builds.plan(revision.id, node, now=now)
    required = parse_stored_build_plan(original.agent_payload).limits.memory_bytes
    with sessions.begin() as session:
        snapshot = session.scalar(
            select(NodeInventorySnapshot).order_by(
                NodeInventorySnapshot.observed_at.desc()
            )
        )
        assert snapshot is not None
        snapshot.host_memory_free_bytes = snapshot.gpu_memory_free_bytes = (
            runtime.nodes[0].required_memory_bytes
            + runtime.nodes[0].memory_floor_bytes
            + required
            + headroom
        )
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    if pool == "shared" and headroom < 0:
        with pytest.raises(RecipeBuildError) as preview_failure:
            builds.plan(revision.id, node, now=now)
        assert preview_failure.value.code == "build.insufficient_memory"
        with pytest.raises(RecipeBuildError) as acceptance_failure:
            operations.build(
                original,
                build_input_sha256=original.build_input_sha256,
                actor="admin",
                request_id=str(uuid4()),
            )
        assert acceptance_failure.value.code == "build.insufficient_memory"
    else:
        builds.plan(revision.id, node, now=now)
        accepted = operations.build(
            original,
            build_input_sha256=original.build_input_sha256,
            actor="admin",
            request_id=str(uuid4()),
        )
        with sessions() as session:
            assert (
                session.scalar(
                    select(ResourceReservation).where(
                        ResourceReservation.owner_id == accepted.owner_id,
                        ResourceReservation.kind == "host-memory",
                        ResourceReservation.state == "active",
                    )
                )
                is not None
            )


@pytest.mark.parametrize("pool", ["shared", "separate"])
def test_unified_demand_checks_each_separate_pool_without_adding_independent_claims(
    tmp_path, postgres_engine, pool
):
    sessions, now, node, installation = run_setup(
        tmp_path, engine=postgres_engine, memory_kind="unified", memory_pool=pool
    )
    for kind in ("host-memory", "gpu-memory"):
        _claim(sessions, node, now, kind=kind, amount=50, owner="recipe-build")
    runs = RunAdmissionService(sessions)
    plan = runs.plan_run(installation, "unified", now=now)
    assert plan.allowed == (pool == "separate")
    if plan.allowed:
        runs.accept_run(plan, actor="admin", now=now)


def test_separate_pools_recheck_the_other_constraint_after_a_planned_stop():
    node = "spk_" + "1" * 32
    capacity = memory_capacity_snapshot(
        node,
        "unified",
        host=(100, 80),
        accelerator=(100, 60),
        reservations=MemoryReservationTotals(
            {"host-memory": 50, "gpu-memory": 10},
            {"host-memory": 50, "gpu-memory": 10},
        ),
        memory_pool="separate",
        evidence_state="fresh",
    )
    demand = ResourceDemand(None, None, None, None, None, 40, "declared")
    plan = plan_capacity(
        {node: demand},
        [capacity],
        [PlannedStopRelease("old-host", node, "host-memory", 50, True)],
        memory_floor_bytes=20,
    )
    # The host becomes roomy after stopping, but the GPU still misses reserve.
    # Reusing only the original limiting constraint would wrongly allow this.
    assert not plan.allowed
    assert plan.nodes[0].after_stop_free_after_bytes == 10


def test_changed_physical_pool_requires_a_new_profile_review(tmp_path, postgres_engine):
    sessions, _, _, profile, api, headers, reviewed, _ = _capacity_profile(
        tmp_path, postgres_engine
    )
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.memory_pool = "separate"
    current = api.post(f"/api/profile/{profile.number}/preview", headers=headers)
    assert current.status_code == 200 and current.json()["allowed"], current.text
    assert current.json()["plan_digest"] != reviewed["plan_digest"]
    refused = api.post(
        f"/api/profile/{profile.number}/load",
        headers=headers,
        json={
            "plan_digest": reviewed["plan_digest"],
            "request_key": str(uuid4()),
        },
    )
    assert refused.status_code == 409, refused.text
