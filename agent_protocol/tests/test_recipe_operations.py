from __future__ import annotations

import json
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    AgentOperation,
    AgentProtocolError,
    RecipeOperationRequest,
    RecipeReconcilePayload,
    RecipeReconcileResult,
    RecipeStopPayload,
    RecipeStopResult,
    RecipeUninstallResult,
    parse_recipe_operation_result,
)

INSTALLATION_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "00000000-0000-4000-8000-000000000003"
RECIPE_DIGEST = "a" * 64
PLAN_DIGEST = "b" * 64
RECONCILE = {"installation_id": INSTALLATION_ID, "plan_digest": PLAN_DIGEST}
STOP = RecipeStopPayload.model_validate(
    {
        "run_id": RUN_ID,
        "target_runtime_id": RUN_ID,
        "run_generation": 1,
        "installation_id": INSTALLATION_ID,
        "recipe_revision_id": "00000000-0000-4000-8000-000000000004",
        "mapping_id": "00000000-0000-4000-8000-000000000005",
        "plan_digest": PLAN_DIGEST,
        "compiled_execution_plan": json.loads(
            (
                Path(__file__).parent / "fixtures" / "compiled-execution-plan-v2.json"
            ).read_text(encoding="utf-8")
        ),
        "cancel_pending_start": False,
    }
).model_dump(mode="json")
UNINSTALL = {
    "installation_id": INSTALLATION_ID,
    "recipe_content_sha256": RECIPE_DIGEST,
    "cleanup_model_content_sha256": None,
    "plan_digest": PLAN_DIGEST,
}
UNINSTALL_WITH_MODEL_CLEANUP = UNINSTALL | {"cleanup_model_content_sha256": "f" * 64}


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        (AgentOperation.RECIPE_STOP, STOP),
        (AgentOperation.RECIPE_UNINSTALL, UNINSTALL),
        (AgentOperation.RECIPE_UNINSTALL, UNINSTALL_WITH_MODEL_CLEANUP),
        (AgentOperation.RECIPE_RECONCILE, RECONCILE),
    ],
)
def test_recipe_operation_payloads_are_typed_and_digest_bound(
    operation: AgentOperation, payload: dict[str, object]
) -> None:
    request = RecipeOperationRequest.parse(operation, payload)

    assert request.operation is operation
    assert request.plan_digest == payload["plan_digest"]


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        (AgentOperation.RECIPE_STOP, STOP | {"plan_digest": "not-a-digest"}),
        (AgentOperation.RECIPE_UNINSTALL, UNINSTALL | {"host_path": "/tmp"}),
    ],
)
def test_recipe_operations_reject_hacks_unknown_fields_and_weak_identity(
    operation: AgentOperation, payload: dict[str, object]
) -> None:
    with pytest.raises(AgentProtocolError):
        RecipeOperationRequest.parse(operation, payload)


def test_uninstall_cleanup_key_is_required_and_nullable() -> None:
    payload = dict(UNINSTALL)
    payload.pop("cleanup_model_content_sha256")
    with pytest.raises(AgentProtocolError):
        RecipeOperationRequest.parse(AgentOperation.RECIPE_UNINSTALL, payload)

    parsed = RecipeOperationRequest.parse(AgentOperation.RECIPE_UNINSTALL, UNINSTALL)
    assert parsed.cleanup_model_content_sha256 is None


def test_reconciliation_payload_names_only_the_installation() -> None:
    parsed = RecipeOperationRequest.parse(AgentOperation.RECIPE_RECONCILE, RECONCILE)
    assert isinstance(parsed.payload, RecipeReconcilePayload)
    with pytest.raises(AgentProtocolError):
        RecipeOperationRequest.parse(
            AgentOperation.RECIPE_RECONCILE, RECONCILE | {"installation_id": "bad-id"}
        )
    with pytest.raises(AgentProtocolError):
        RecipeOperationRequest.parse(AgentOperation.RECIPE_UNINSTALL, RECONCILE)


@pytest.mark.parametrize(
    ("operation", "body", "result_type"),
    [
        (AgentOperation.RECIPE_STOP, {}, RecipeStopResult),
        (AgentOperation.RECIPE_UNINSTALL, {}, RecipeUninstallResult),
        (AgentOperation.RECIPE_RECONCILE, {}, RecipeReconcileResult),
    ],
)
def test_recipe_success_results_are_operation_specific(
    operation: AgentOperation, body: dict[str, object], result_type: type
) -> None:
    assert isinstance(parse_recipe_operation_result(operation, body), result_type)
    with pytest.raises(AgentProtocolError):
        parse_recipe_operation_result(operation, body | {"unexpected": True})
