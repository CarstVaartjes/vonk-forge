"""Canonical typed Stop payloads for generic operation-queue tests."""

from __future__ import annotations

from pathlib import Path

from vonk_agent_protocol import CompiledExecutionPlan, RecipeStopPayload

_RUN_ID = "00000000-0000-4000-8000-000000000001"
_INSTALLATION_ID = "00000000-0000-4000-8000-000000000002"
_RECIPE_REVISION_ID = "00000000-0000-4000-8000-000000000003"
_MAPPING_ID = "00000000-0000-4000-8000-000000000004"
_COMPILED_PLAN = CompiledExecutionPlan.model_validate_json(
    (Path(__file__).parent / "fixtures/compiled_workload_v2.json").read_text(
        encoding="utf-8"
    )
)


def recipe_stop_payload(node_id: str, *, plan_digest: str) -> dict[str, object]:
    """Return a typed current-contract Stop payload (the node is the claimant)."""

    del node_id
    return RecipeStopPayload(
        run_id=_RUN_ID,
        target_runtime_id=_RUN_ID,
        run_generation=1,
        installation_id=_INSTALLATION_ID,
        recipe_revision_id=_RECIPE_REVISION_ID,
        mapping_id=_MAPPING_ID,
        plan_digest=plan_digest,
        compiled_execution_plan=_COMPILED_PLAN,
        cancel_pending_start=True,
    ).model_dump(mode="json")
