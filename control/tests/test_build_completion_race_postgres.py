"""Image planning consumes a build that completes during its own lookup."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentOperation, LifecycleState
from vonk_control.models import (
    AgentNode,
    CatalogDocumentRevision,
    NodeInventorySnapshot,
    RecipeBuild,
)
from vonk_control.recipe_builds import RecipeBuildPlan
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_recipe_operations import setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _request,
)


@pytest.mark.parametrize("inspection", [False, True])
def test_build_completed_during_lookup_is_available(
    tmp_path, postgres_engine, monkeypatch, inspection
):
    """Catches returning build=None for a freshly refreshed succeeded receipt."""
    sessions, lifecycle, _, _, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        build.state = LifecycleState.FAILED
        revision = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert revision is not None
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        node.binary_digest = "a" * 64
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == nodes[0]
            )
        )
        assert snapshot is not None
        snapshot.capabilities = [*snapshot.capabilities, AgentOperation.RECIPE_BUILD]
        proposed = RecipeBuildPlan(
            build_id=build.id,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            builder_node_id=build.builder_node_id,
            source_bundle_sha256=build.source_bundle_sha256,
            build_input_sha256=build.build_input_sha256,
            agent_payload=dict(build.plan),
            policy_report=dict(build.policy_report),
        )

    def finish_during_preview(recipe_revision_id, builder_node_id):
        with sessions.begin() as session:
            completed = session.get(RecipeBuild, build_id)
            assert completed is not None
            completed.state = LifecycleState.SUCCEEDED
        return proposed

    if inspection:
        lookups = 0

        def reusable_build_id(recipe_revision_id):
            nonlocal lookups
            lookups += 1
            if lookups == 2:
                finish_during_preview(recipe_revision_id, nodes[0])
            return build_id

        monkeypatch.setattr(lifecycle, "reusable_build_id", reusable_build_id)
    else:
        monkeypatch.setattr(lifecycle, "preview_build", finish_during_preview)
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=0,
    )
    inspect = service.inspect_request if inspection else service.preview
    plan = inspect(_request(sessions, nodes[0]), actor="admin")
    assert plan.allowed, plan.blockers
    assert plan.build.build_id == build_id
    assert plan.image_digest is not None
