from __future__ import annotations

import pytest
from vonk_agent_protocol import (
    AgentOperation,
    AgentProtocolError,
    RecipeOperationRequest,
    RecipeReconcilePayload,
    RecipeReconcileResult,
    RecipeStopResult,
    RecipeUninstallResult,
    parse_recipe_operation_result,
)

INSTALLATION_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "00000000-0000-4000-8000-000000000003"
RECIPE_DIGEST = "a" * 64
PLAN_DIGEST = "b" * 64
NODE_ID = "spk_" + "c" * 32
RECONCILE = {
    "schema_version": 1,
    "node_id": NODE_ID,
    "installation_id": INSTALLATION_ID,
    "install_operation_id": RUN_ID,
    "install_operation_payload_sha256": "d" * 64,
    "plan_digest": PLAN_DIGEST,
    "recipe_revision_id": "00000000-0000-4000-8000-000000000004",
    "recipe_content_sha256": RECIPE_DIGEST,
    "compiled_spec_canonical_sha256": "e" * 64,
}
STOP = {
    "schema_version": 1,
    "run_id": RUN_ID,
    "plan_digest": PLAN_DIGEST,
}
UNINSTALL = {
    "schema_version": 1,
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


def test_reconciliation_payload_requires_exact_node_and_original_install_identity() -> None:
    parsed = RecipeOperationRequest.parse(AgentOperation.RECIPE_RECONCILE, RECONCILE)
    assert isinstance(parsed.payload, RecipeReconcilePayload)
    for change in (
        {"node_id": "spk_" + "f" * 31},
        {"install_operation_payload_sha256": "invalid"},
        {"compiled_spec_canonical_sha256": "F" * 64},
        {"installation_id": "bad-id"},
    ):
        with pytest.raises(AgentProtocolError):
            RecipeOperationRequest.parse(
                AgentOperation.RECIPE_RECONCILE, RECONCILE | change
            )
    with pytest.raises(AgentProtocolError):
        RecipeOperationRequest.parse(
            AgentOperation.RECIPE_UNINSTALL, RECONCILE
        )


@pytest.mark.parametrize(
    ("operation", "body", "result_type"),
    [
        (AgentOperation.RECIPE_STOP, {"stopped": True}, RecipeStopResult),
        (
            AgentOperation.RECIPE_UNINSTALL,
            {"uninstalled": True, "removed_model_bytes": 0},
            RecipeUninstallResult,
        ),
        (
            AgentOperation.RECIPE_RECONCILE,
            {
                "reconciled": True,
                **{
                    key: value
                    for key, value in RECONCILE.items()
                    if key != "schema_version"
                },
                "removed_bytes": 18,
                "cleanup_receipt_sha256": "f" * 64,
            },
            RecipeReconcileResult,
        ),
    ],
)
def test_recipe_success_results_are_operation_specific(
    operation: AgentOperation, body: dict[str, object], result_type: type
) -> None:
    assert isinstance(parse_recipe_operation_result(operation, body), result_type)
    with pytest.raises(AgentProtocolError):
        parse_recipe_operation_result(operation, body | {"unexpected": True})
