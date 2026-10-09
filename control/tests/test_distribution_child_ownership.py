"""Regress the reader dispatch that hid completed recipe children in Release."""

from pathlib import Path
from uuid import uuid4

import pytest
from vonk_agent_protocol import AgentInstallResult, LifecycleState, RecipeStopResult
from vonk_agent_protocol.agent_words import FailureStage, ProfileEffectState
from vonk_control.agent_jobs import AgentJobService
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.distribution import DistributionService, MemoryObjectSource
from vonk_control.distribution_executor import (
    CompositeDistributionPhaseExecutor,
    DurableDistributionPhaseExecutor,
)
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Job
from vonk_control.recipe_operations import RecipeOperationView
from vonk_control.run_switch_operations import RecipeLifecyclePhaseExecutor

from .test_recipe_operations import NOW, complete_started_recipe, setup_services


@pytest.mark.parametrize("composite", [False, True])
def test_recipe_children_reach_their_owner_and_fresh_run_is_admitted(
    tmp_path: Path, composite: bool
) -> None:
    """Catch a distribution miss shadowing install/start/stop's actual receipt.

    Use the production reader chain, cache and lifecycle store. Observe a
    pending child, complete its real effects, then observe its terminal receipt
    through that same chain. A subsequent run must be admitted after Stop.
    """
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    operations = AgentJobService(sessions, clock=lambda: NOW)
    distribution = DistributionService(MemoryObjectSource(), sessions=sessions)
    artifact = (
        CompositeDistributionPhaseExecutor(
            sessions,
            operations,
            distribution,
            model_cache=ModelCacheService(
                sessions, tmp_path / "cache", reserve_bytes=0
            ),
            clock=lambda: NOW,
        )
        if composite
        else DurableDistributionPhaseExecutor(
            sessions, operations, distribution, clock=lambda: NOW
        )
    )
    reader = RecipeLifecyclePhaseExecutor(
        lifecycle,
        sessions,
        ClusterMappingService(sessions),
        lambda: NOW,
        artifact_executor=artifact,
    )

    def observe(operation_id: str, state: LifecycleState) -> RecipeOperationView:
        view = reader.get(operation_id)
        assert isinstance(view, RecipeOperationView)
        assert view.id == operation_id
        assert view.state == state
        assert view.lifecycle_result == lifecycle.get(operation_id).lifecycle_result
        return view

    try:
        plan = lifecycle.preview_install(mapping_id, build_id)
        installation = lifecycle.install(
            plan,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=str(uuid4()),
        )
        observe(installation.id, LifecycleState.RUNNING)
        for node_id in nodes:
            lifecycle.record_node_result(
                installation.id,
                node_id,
                succeeded=True,
                evidence=AgentInstallResult(installed_bytes=120).model_dump(
                    mode="json"
                ),
            )
        assert observe(installation.id, LifecycleState.SUCCEEDED).lifecycle_result

        run_plan = lifecycle.preview_run(installation.owner_id, "qwen")
        run = lifecycle.start(
            run_plan,
            plan_digest=run_plan.plan_digest,
            actor="admin",
            request_id=str(uuid4()),
        )
        observe(run.id, LifecycleState.RUNNING)
        complete_started_recipe(sessions, lifecycle, run.id)
        assert observe(run.id, LifecycleState.SUCCEEDED).lifecycle_result

        stop_plan = lifecycle.preview_stop(run.owner_id)
        stopped = lifecycle.stop(
            run.owner_id,
            plan_digest=stop_plan.plan_digest,
            actor="admin",
            request_id=str(uuid4()),
        )
        observe(stopped.id, LifecycleState.RUNNING)
        for node_id in nodes:
            lifecycle.record_node_result(
                stopped.id,
                node_id,
                succeeded=True,
                evidence=RecipeStopResult().model_dump(mode="json"),
            )
        assert observe(stopped.id, LifecycleState.SUCCEEDED).lifecycle_result

        fresh_plan = lifecycle.preview_run(installation.owner_id, "qwen")
        fresh = lifecycle.start(
            fresh_plan,
            plan_digest=fresh_plan.plan_digest,
            actor="admin",
            request_id=str(uuid4()),
        )
        assert fresh.id != run.id
        observe(fresh.id, LifecycleState.RUNNING)
        complete_started_recipe(sessions, lifecycle, fresh.id)
        assert observe(fresh.id, LifecycleState.SUCCEEDED).lifecycle_result
    finally:
        if isinstance(artifact, CompositeDistributionPhaseExecutor):
            artifact.close()


@pytest.mark.usefixtures("damaged_json_rows")
def test_owned_distribution_without_receipts_stays_with_distribution(
    tmp_path: Path,
) -> None:
    """An owned unknown must not fall through and become a recipe operation."""
    sessions, _lifecycle, _queue, _mapping_id, _build_id, _nodes = setup_services(
        tmp_path
    )
    operation_id = str(uuid4())
    with sessions.begin() as session:
        session.add(
            Job(
                id=operation_id,
                request_id=str(uuid4()),
                kind=FailureStage.ARTIFACT_DISTRIBUTION,
                state=LifecycleState.RUNNING,
                actor="admin",
                authority_revision="a" * 64,
                payload_digest="b" * 64,
                targets=[],
                payload={},
                created_at=NOW,
                updated_at=NOW,
            )
        )
    artifact = DurableDistributionPhaseExecutor(
        sessions,
        AgentJobService(sessions, clock=lambda: NOW),
        DistributionService(MemoryObjectSource(), sessions=sessions),
        clock=lambda: NOW,
    )
    observed = artifact.get(operation_id)
    assert observed is not None
    assert observed.state == ProfileEffectState.UNKNOWN
    assert artifact.get(str(uuid4())) is None
    with sessions() as session:
        stored = session.get(Job, operation_id)
        assert stored is not None and stored.state == LifecycleState.RUNNING
