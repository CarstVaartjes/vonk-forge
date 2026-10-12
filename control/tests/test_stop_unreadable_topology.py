"""A running workload stays stoppable when its recipe can no longer be read."""

from uuid import uuid4

from sqlalchemy import select
from vonk_agent_protocol import canonical_message
from vonk_control import recipe_progress
from vonk_control.job_documents import RecipeStopParent
from vonk_control.models import AgentOperation, Job, RecipeRun
from vonk_control.recipe_runtime_specs import RecipeRuntimeSpecError

from .test_recipe_operations import installed_recipe, setup_services, started_recipe


def test_run_whose_recipe_topology_became_unreadable_is_still_stopped(
    tmp_path, monkeypatch
) -> None:
    withdrawn: list[str] = []
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, route_withdrawer=withdrawn.append
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id=str(uuid4())
    )
    run = started_recipe(
        sessions, service, installation.owner_id, nodes, request_id=str(uuid4())
    )
    with sessions.begin() as session:
        stored_run = session.get(RecipeRun, run.owner_id)
        assert stored_run is not None
        stored_run.route_state = "published"

    def unreadable(_document: object) -> object:
        raise RecipeRuntimeSpecError("recipe does not satisfy RecipeDefinition v2")

    # The stored recipe revision no longer parses under the current contract
    # (the contract moved on after the run started): its topology, and so its
    # role stop order, cannot be derived any more.
    monkeypatch.setattr(recipe_progress, "recipe_topology", unreadable)

    plan = service.preview_stop(run.owner_id)
    stopped = service.stop(
        run.owner_id,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )

    assert stopped.state == "running", stopped
    assert withdrawn == [run.owner_id]
    with sessions() as session:
        job = session.get(Job, stopped.id)
        assert job is not None
        parent = RecipeStopParent.model_validate_json(canonical_message(job.payload))
        review = parent.service_stop_review
        assert review is not None and review.stage == "dispatched"
        assert review.stop_order is None and review.exact_payloads is not None
        children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
            )
        )
        # The targets are stopped together from the run's accepted Stop payloads.
        assert {child.node_id for child in children} == set(nodes)
        for child in children:
            assert canonical_message(child.payload) == canonical_message(
                review.exact_payloads[child.node_id]
            )

    for node in nodes:
        service.record_node_result(stopped.id, node, succeeded=True, evidence={})
    assert service.get(stopped.id).state == "succeeded"
    # The lane is free again: a fresh Start of the same installation is admitted.
    monkeypatch.undo()
    fresh_plan = service.preview_run(installation.owner_id, "qwen")
    assert fresh_plan.allowed
    restarted = service.start(
        fresh_plan,
        plan_digest=fresh_plan.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )
    assert restarted.state == "running"
