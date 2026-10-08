"""Receipt encoding regressions independent of the Linux Rust probe lane."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from vonk_agent_protocol import RecipeStartResult, canonical_message
from vonk_control.bounded_json import require_mapping
from vonk_control.models import AgentOperation, Job

from .test_recipe_operations import installed_recipe, setup_services, start_evidence


@pytest.mark.parametrize("nodes", [1, 2])
def test_start_receipts_keep_the_agent_canonical_encoding(tmp_path, nodes):
    """Catch full dumps adding optional nulls to persisted and public receipts."""
    sessions, service, _queue, mapping_id, build_id, node_ids = setup_services(
        tmp_path, nodes=nodes
    )
    installation = installed_recipe(
        service, mapping_id, build_id, node_ids, request_id="1" * 36
    )
    plan = service.preview_run(installation.owner_id, "canonical-receipt")
    start = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="2" * 36
    )
    for node_id in node_ids:
        with sessions() as session:
            child = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == start.id,
                    AgentOperation.node_id == node_id,
                )
            )
            assert child is not None
            receipt = RecipeStartResult.model_validate_json(
                canonical_message(start_evidence(child.payload))
            )
        wire = json.loads(canonical_message(receipt))
        view = service.record_node_result(
            start.id, node_id, succeeded=True, evidence=wire
        )
        with sessions() as session:
            job = session.get(Job, start.id)
            assert job is not None
            stored = require_mapping(job.result, "stored receipt")
            assert (
                require_mapping(stored["launch_evidence"], "launch receipts")[node_id]
                == wire
            )
        public = require_mapping(view.result, "public receipt")
        assert (
            require_mapping(public["launch_evidence"], "launch receipts")[node_id]
            == wire
        )
        assert view.result == json.loads(canonical_message(view.lifecycle_result))


def test_full_dumps_are_only_for_controller_owned_state():
    """Catch a wire receipt/payload bypassing canonical serialization after a move."""
    import ast
    from pathlib import Path

    package = Path(__file__).parents[1] / "src/vonk_control/recipe_operations"
    controller_models = {
        "RecipeBuildParent",
        "ProfileJobRunStopJob",
    }
    controller_values = {
        ("build.py", "intent"),
        ("build_cancellation.py", "cancellation"),
        ("persistence.py", "context"),
    }
    full_dumps = []
    for path in package.glob("*.py"):
        for call in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "model_dump"
            ):
                continue
            full_dumps.append(call)
            value = call.func.value
            if isinstance(value, ast.Name):
                assert (path.name, value.id) in controller_values
                continue
            assert isinstance(value, ast.Call), path.name
            if isinstance(value.func, ast.Attribute):
                owner = value.func.value
            else:
                assert (
                    isinstance(value.func, ast.Name)
                    and value.func.id == "read_stored_model"
                ), path.name
                owner = value.args[0]
            assert isinstance(owner, ast.Name) and owner.id in controller_models, (
                path.name,
                ast.unparse(value),
            )
    # This ceiling can only fall as Controller state gains a dedicated encoder.
    assert len(full_dumps) <= 8
