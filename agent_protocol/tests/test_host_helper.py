from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import (
    AgentProtocolError,
    RecipeRunObservationReceiptClaims,
    RecipeStartPayload,
    RecipeStopPayload,
    SignedRecipeRunObservationReceipt,
    canonical_message,
    recipe_run_observation_receipt_signing_bytes,
)
from vonk_agent_protocol.host_helper import (
    ExecuteContainerRuntimeRequestOperation,
    HostHelperSignature,
    HostRuntimeRequest,
)


def start_plan(*, large_environment: bool = False) -> RecipeStartPayload:
    plan = json.loads(
        (
            Path(__file__).parent / "fixtures" / "compiled-execution-plan-v2.json"
        ).read_text(encoding="utf-8")
    )
    plan["runtime"]["placement"]["endpoint_address"] = "100.100.20.30"
    plan["security"]["network_mode"] = "bridge"
    if large_environment:
        plan["runtime"]["env"].extend(
            {"name": f"PROFILE_STOP_TEST_{index:03}", "value": "x" * 60000}
            for index in range(40)
        )
    placement = plan["runtime"]["placement"]
    return RecipeStartPayload.model_validate(
        {
            "schema_version": 2,
            "run_id": "00000000-0000-4000-8000-000000000003",
            "installation_id": "00000000-0000-4000-8000-000000000001",
            "recipe_revision_id": "00000000-0000-4000-8000-000000000002",
            "recipe_content_sha256": plan["identity"]["recipe_revision_sha256"],
            "mapping_id": "00000000-0000-4000-8000-000000000007",
            "mapping_generation": 1,
            "image_digest": plan["runtime_image"]["image_digest"],
            "plan_digest": "c" * 64,
            "alias": "test-model",
            "rank": placement["rank"],
            "role": placement["role"],
            "port": 8000,
            "reserved_memory_bytes": placement["reserved_memory_bytes"],
            "memory_floor_bytes": placement["memory_floor_bytes"],
            "memory_kind": "unified",
            "endpoint_address": "100.100.20.30",
            "world_size": placement["world_size"],
            "compiled_execution_plan": plan,
            "local_address": None,
            "master_address": None,
            "master_port": None,
            "run_generation": 1,
        }
    )


def stop_plan() -> RecipeStopPayload:
    start = start_plan()
    return RecipeStopPayload.model_validate(
        {
            "schema_version": 2,
            "run_id": start.run_id,
            "target_runtime_id": start.run_id,
            "run_generation": start.run_generation,
            "node_id": "spk_" + "a" * 32,
            "installation_id": start.installation_id,
            "recipe_revision_id": start.recipe_revision_id,
            "recipe_content_sha256": start.recipe_content_sha256,
            "mapping_id": start.mapping_id,
            "mapping_generation": start.mapping_generation,
            "plan_digest": start.plan_digest,
            "rank": start.rank,
            "role": start.role,
            "world_size": start.world_size,
            "compiled_execution_plan": start.compiled_execution_plan,
            "cancel_pending_start": False,
        }
    )


def test_exact_observation_receipt_is_strict_domain_separated_and_signed() -> None:
    claims = RecipeRunObservationReceiptClaims(
        schema_version=1,
        authority="vonk.recipe-run-observation-helper",
        node_id="spk_" + "a" * 32,
        request_id="10000000-0000-4000-8000-000000000001",
        request_sha256="b" * 64,
        observation_identity_sha256="c" * 64,
        outcome="running",
        observed_at=1_788_189_600,
    )
    signed = SignedRecipeRunObservationReceipt(
        schema_version=1,
        claims=claims,
        signature=HostHelperSignature(
            algorithm="ed25519",
            key_id="d" * 64,
            value="e" * 128,
        ),
    )

    parsed = SignedRecipeRunObservationReceipt.parse(signed.to_mapping())
    assert parsed == signed
    assert recipe_run_observation_receipt_signing_bytes(claims).startswith(
        b"VONK-RECIPE-RUN-OBSERVATION-RECEIPT-V1\x00"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("authority", "vonk.host-maintenance-helper"),
        ("outcome", "ready"),
        ("observed_at", 0),
        ("request_sha256", "B" * 64),
    ),
)
def test_exact_observation_receipt_rejects_invalid_claims(
    field: str, value: object
) -> None:
    document = {
        "schema_version": 1,
        "authority": "vonk.recipe-run-observation-helper",
        "node_id": "spk_" + "a" * 32,
        "request_id": "10000000-0000-4000-8000-000000000001",
        "request_sha256": "b" * 64,
        "observation_identity_sha256": "c" * 64,
        "outcome": "running",
        "observed_at": 1_788_189_600,
    }
    document[field] = value

    with pytest.raises(AgentProtocolError):
        RecipeRunObservationReceiptClaims.parse(document)


def test_rust_signed_receipt_fixture_round_trips_with_identical_signing_bytes() -> None:
    raw = (
        (Path(__file__).parents[1] / "fixtures" / "recipe-run-observation-receipt.json")
        .read_bytes()
        .rstrip(b"\n")
    )
    document = json.loads(raw)
    receipt = SignedRecipeRunObservationReceipt.parse(document)

    assert canonical_message(receipt.to_mapping()) == raw
    assert recipe_run_observation_receipt_signing_bytes(receipt.claims) == (
        b"VONK-RECIPE-RUN-OBSERVATION-RECEIPT-V1\x00"
        + b'{"authority":"vonk.recipe-run-observation-helper",'
        b'"node_id":"spk_0123456789abcdef0123456789abcdef",'
        b'"observation_identity_sha256":"' + b"b" * 64 + b'",'
        b'"observed_at":1788000000,"outcome":"not-running",'
        b'"request_id":"10000000-0000-4000-8000-000000000001",'
        b'"request_sha256":"' + b"a" * 64 + b'","schema_version":1}'
    )


def test_runtime_request_arguments_are_bounded_by_bytes_not_a_count() -> None:
    """A many-mount command line the plan admits must survive the request model.

    Wrong implementations this catches: a 4096-element ``maxItems`` ceiling
    refused a legitimate many-mount command line the byte budget had room for,
    and the item pattern refused the empty element the plan's opaque-argv
    contract permits.
    """
    from vonk_agent_protocol.contracts import MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES
    from vonk_agent_protocol.host_helper import (
        HOST_RUNTIME_REQUEST_ENVELOPE_BYTES,
        MAX_ARGV_BYTES,
        MAX_HELPER_FRAME_BYTES,
        MAX_HOST_RUNTIME_ARGUMENT_BYTES,
        MAX_HOST_RUNTIME_REQUEST_BYTES,
    )

    assert MAX_HOST_RUNTIME_REQUEST_BYTES == (
        MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES + MAX_HELPER_FRAME_BYTES
    )
    assert MAX_HOST_RUNTIME_ARGUMENT_BYTES == (
        MAX_HELPER_FRAME_BYTES - HOST_RUNTIME_REQUEST_ENVELOPE_BYTES
    )
    assert MAX_ARGV_BYTES == MAX_HOST_RUNTIME_ARGUMENT_BYTES
    document = {
        "schema_version": 1,
        "action": "start",
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 1,
        "fence": "40000000-0000-4000-8000-000000000004",
        "run_generation": 1,
        "start_plan": start_plan().model_dump(mode="json"),
        "arguments": [
            *(f"--mount=type=bind,src={index:05}" for index in range(6000)),
            "",
            "line one\nline two\r\n",
        ],
    }
    request = HostRuntimeRequest.model_validate(document)
    assert len(request.arguments) == 6002
    assert len(canonical_message(request)) < MAX_HOST_RUNTIME_REQUEST_BYTES


def test_runtime_stop_request_binds_exact_stop_plan_and_has_no_argv() -> None:
    stop = stop_plan()
    request = HostRuntimeRequest.model_validate(
        {
            "schema_version": 1,
            "action": "stop",
            "job_id": "20000000-0000-4000-8000-000000000002",
            "operation_id": "30000000-0000-4000-8000-000000000003",
            "attempt": 1,
            "fence": "40000000-0000-4000-8000-000000000004",
            "arguments": [],
            "run_generation": stop.run_generation,
            "stop_plan": stop.model_dump(mode="json"),
        }
    )

    assert request.stop_plan == stop
    with pytest.raises(ValidationError, match="runtime arguments"):
        HostRuntimeRequest.model_validate(
            {
                "schema_version": 1,
                "action": "stop",
                "job_id": "20000000-0000-4000-8000-000000000002",
                "operation_id": "30000000-0000-4000-8000-000000000003",
                "attempt": 1,
                "fence": "40000000-0000-4000-8000-000000000004",
                "arguments": ["docker", "stop"],
                "run_generation": stop.run_generation,
                "stop_plan": stop.model_dump(mode="json"),
            }
        )


def test_exact_runtime_request_round_trips_a_plan_over_two_megabytes() -> None:
    start = start_plan(large_environment=True)
    document = {
        "schema_version": 1,
        "action": "start",
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 1,
        "fence": "40000000-0000-4000-8000-000000000004",
        "arguments": ["runtime"],
        "run_generation": start.run_generation,
        "start_plan": start.model_dump(mode="json"),
    }

    request = HostRuntimeRequest.model_validate(document)
    encoded = canonical_message(request)
    assert len(encoded) > 2 * 1024 * 1024
    assert HostRuntimeRequest.model_validate_json(encoded) == request


def test_runtime_request_rejects_a_typed_plan_above_its_document_ceiling() -> None:
    start = start_plan()
    plan = start.compiled_execution_plan.model_dump(mode="json")
    extra_environment_count = 128 - len(plan["runtime"]["env"])
    plan["runtime"]["env"].extend(
        {"name": f"PROFILE_STOP_TEST_{index:03}", "value": "x" * 65000}
        for index in range(extra_environment_count)
    )
    template = plan["artifacts"][0]
    plan["artifacts"] = [
        {
            **template,
            "file_id": f"file-{index}",
            "path": f"{index:04}" + "模" * 508,
            "model": {**template["model"], "publisher": "发" * 128},
        }
        for index in range(4096)
    ]
    oversized = start.model_dump(mode="json")
    oversized["compiled_execution_plan"] = plan
    document = {
        "schema_version": 1,
        "action": "start",
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 1,
        "fence": "40000000-0000-4000-8000-000000000004",
        "arguments": ["runtime"],
        "run_generation": start.run_generation,
        "start_plan": oversized,
    }

    with pytest.raises(ValidationError, match="compiled plan exceeds its byte ceiling"):
        HostRuntimeRequest.model_validate(document)


@pytest.mark.parametrize("action", ["start", "stop"])
def test_runtime_authority_grant_binds_plan_generation_and_both_run_ids(
    action: str,
) -> None:
    plan_fields = {
        "start_plan_sha256": "a" * 64 if action == "start" else None,
        "stop_plan_sha256": "b" * 64 if action == "stop" else None,
        "run_generation": 7,
        "runtime_run_id": "50000000-0000-4000-8000-000000000005",
        "runtime_target_id": "60000000-0000-4000-8000-000000000006",
        "runtime_installation_id": "70000000-0000-4000-8000-000000000007",
    }
    valid = {
        "type": "execute-container-runtime-request",
        "action": action,
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 1,
        "fence": "40000000-0000-4000-8000-000000000004",
        "request_sha256": "c" * 64,
        **plan_fields,
    }
    ExecuteContainerRuntimeRequestOperation.model_validate(valid)
    with pytest.raises(ValidationError, match="authority"):
        ExecuteContainerRuntimeRequestOperation.model_validate(
            valid | {"runtime_target_id": None}
        )


@pytest.mark.parametrize(
    "model", [HostRuntimeRequest, ExecuteContainerRuntimeRequestOperation]
)
def test_runtime_cleanup_identity_is_required_only_for_cleanup(model) -> None:
    document = {
        "action": "installation-cleanup",
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 2,
        "fence": "40000000-0000-4000-8000-000000000004",
    }
    if model is HostRuntimeRequest:
        document.update(schema_version=1, arguments=[])
    else:
        document.update(
            type="execute-container-runtime-request", request_sha256="a" * 64
        )
    installation_id = "70000000-0000-4000-8000-000000000007"
    valid = model.model_validate(document | {"installation_id": installation_id})
    assert json.loads(canonical_message(valid))["installation_id"] == installation_id
    for missing in ({}, {"installation_id": None}):
        with pytest.raises(ValidationError, match="installation identity"):
            model.model_validate(document | missing)
    if model is HostRuntimeRequest:
        with pytest.raises(ValidationError, match="runtime arguments"):
            model.model_validate(
                document | {"installation_id": installation_id, "arguments": ["rm"]}
            )
    ordinary = document | {"action": "runtime-preflight"}
    with pytest.raises(ValidationError, match="installation identity"):
        model.model_validate(ordinary | {"installation_id": installation_id})
    omitted = model.model_validate(ordinary)
    explicit_null = model.model_validate(ordinary | {"installation_id": None})
    assert canonical_message(omitted) == canonical_message(explicit_null)
    assert "installation_id" not in json.loads(canonical_message(explicit_null))


def test_runtime_reconciliation_identity_is_bound_to_cleanup_request_and_grant() -> (
    None
):
    from vonk_agent_protocol import RecipeReconciliationIdentity

    identity = {
        "schema_version": 1,
        "node_id": "spk_" + "a" * 32,
        "installation_id": "70000000-0000-4000-8000-000000000007",
        "install_operation_id": "80000000-0000-4000-8000-000000000008",
        "install_operation_payload_sha256": "b" * 64,
        "plan_digest": "c" * 64,
        "recipe_revision_id": "90000000-0000-4000-8000-000000000009",
        "recipe_content_sha256": "d" * 64,
        "compiled_spec_canonical_sha256": "e" * 64,
    }
    typed = RecipeReconciliationIdentity.model_validate(identity)
    request = HostRuntimeRequest.model_validate(
        {
            "schema_version": 1,
            "action": "installation-cleanup",
            "job_id": "20000000-0000-4000-8000-000000000002",
            "operation_id": "30000000-0000-4000-8000-000000000003",
            "attempt": 2,
            "fence": "40000000-0000-4000-8000-000000000004",
            "arguments": [],
            "installation_id": identity["installation_id"],
            "reconciliation_identity": identity,
        }
    )
    operation = ExecuteContainerRuntimeRequestOperation.model_validate(
        {
            "type": "execute-container-runtime-request",
            "action": "installation-cleanup",
            "job_id": "20000000-0000-4000-8000-000000000002",
            "operation_id": "30000000-0000-4000-8000-000000000003",
            "attempt": 2,
            "fence": "40000000-0000-4000-8000-000000000004",
            "request_sha256": "f" * 64,
            "installation_id": identity["installation_id"],
            "reconciliation_identity": identity,
        }
    )
    assert request.reconciliation_identity == typed
    assert operation.reconciliation_identity == typed
    with pytest.raises(ValidationError, match="reconciliation identity"):
        HostRuntimeRequest.model_validate(
            {
                "schema_version": 1,
                "action": "installation-cleanup",
                "job_id": "20000000-0000-4000-8000-000000000002",
                "operation_id": "30000000-0000-4000-8000-000000000003",
                "attempt": 2,
                "fence": "40000000-0000-4000-8000-000000000004",
                "arguments": [],
                "installation_id": identity["installation_id"],
                "reconciliation_identity": identity
                | {"installation_id": "70000000-0000-4000-8000-000000000010"},
            }
        )
