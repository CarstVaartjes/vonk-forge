from __future__ import annotations

import pytest
from pydantic import ValidationError
from vonk_control.recipe_api import (
    InstallPlanResponse,
    OperationResponse,
    UninstallPlanResponse,
)
from vonk_control.recipe_lifecycle_contract import RecipeOperationConflictResponse

NODE = "spk_" + "1" * 32
UUID = "00000000-0000-4000-8000-000000000001"


def _operation(result: object) -> dict[str, object]:
    return {
        "id": UUID,
        "kind": "recipe.install",
        "owner_id": UUID,
        "state": "succeeded",
        "plan_digest": "a" * 64,
        "nodes": [NODE],
        "result": result,
    }


def test_lifecycle_response_schema_references_nested_public_contracts() -> None:
    install_schema = InstallPlanResponse.model_json_schema()
    assert install_schema["properties"]["compiled_execution_plans"]["patternProperties"][
        "^spk_[0-9a-f]{32}$"
    ]["$ref"].endswith("CompiledExecutionPlan")

    uninstall_schema = UninstallPlanResponse.model_json_schema()
    assert uninstall_schema["properties"]["recipe_content"]["$ref"].endswith(
        "RecipeDefinition"
    )

    result_schema = OperationResponse.model_json_schema()["properties"]["result"]
    assert all(
        branch.get("type") != "object"
        for branch in result_schema["anyOf"]
        if branch.get("type") is not None
    )


def test_operation_result_validates_actual_aggregate_and_rejects_coercion() -> None:
    operation = OperationResponse.model_validate(
        _operation(
            {
                "successful_nodes": [NODE],
                "failed_nodes": [],
                "node_evidence": {NODE: {"installed_bytes": 120}},
            }
        )
    )
    assert operation.result is not None
    assert operation.result.model_dump(mode="json")["node_evidence"][NODE] == {
        "installed_bytes": 120
    }

    with pytest.raises(ValidationError):
        OperationResponse.model_validate(
            _operation(
                {
                    "successful_nodes": [NODE],
                    "failed_nodes": [],
                    "node_evidence": {NODE: {"installed_bytes": "120"}},
                }
            )
        )


def test_operation_result_selects_logical_and_kind_specific_variants() -> None:
    stopped = _operation({"stopped": True}) | {"kind": "recipe.stop"}
    assert OperationResponse.model_validate(stopped).result is not None

    activated = _operation({"activated": True}) | {"kind": "recipe.job.activate.v1"}
    assert OperationResponse.model_validate(activated).result is not None

    with pytest.raises(ValidationError):
        OperationResponse.model_validate(
            _operation({"node_evidence": {NODE: {"stopped": True}}})
        )


def test_conflict_response_contract_preserves_required_json_shape() -> None:
    body = RecipeOperationConflictResponse.model_validate(
        {
            "code": "recipe.operation_conflict",
            "detail": "submitted plan is stale",
            "request_id": UUID,
        }
    )
    assert body.model_dump(mode="json") == {
        "code": "recipe.operation_conflict",
        "detail": "submitted plan is stale",
        "request_id": UUID,
    }

    with pytest.raises(ValidationError):
        RecipeOperationConflictResponse.model_validate(
            {"code": "recipe.operation_conflict", "detail": "missing request id"}
        )
