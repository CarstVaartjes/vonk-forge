from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import (
    AgentProtocolError,
    RecipeRunObservationReceiptClaims,
    SignedRecipeRunObservationReceipt,
    canonical_message,
    host_artifact_signing_bytes,
    recipe_run_observation_receipt_signing_bytes,
)
from vonk_agent_protocol.host_helper import (
    ExecuteContainerRuntimeRequestOperation,
    HostHelperSignature,
    HostRuntimeRequest,
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


def test_host_artifact_signing_bytes_keep_the_domain_and_raw_digest_contract() -> None:
    assert host_artifact_signing_bytes("agent", "a" * 64) == (
        b"VONK-HOST-ARTIFACT-V1\x00agent\x00" + bytes.fromhex("a" * 64)
    )


def test_runtime_request_arguments_are_bounded_by_bytes_not_a_count() -> None:
    """A many-mount command line the plan admits must survive the request model.

    Wrong implementations this catches: a 4096-element ``maxItems`` ceiling
    refused a legitimate many-mount command line the byte budget had room for,
    and the item pattern refused the empty element the plan's opaque-argv
    contract permits.
    """
    from vonk_agent_protocol.host_helper import (
        HOST_RUNTIME_REQUEST_ENVELOPE_BYTES,
        MAX_ARGV_BYTES,
        MAX_HELPER_FRAME_BYTES,
        MAX_HOST_RUNTIME_REQUEST_BYTES,
    )

    # The request ceiling, not the frame ceiling, is the request's budget; the
    # plan's argv budget derives strictly below it.
    assert MAX_HOST_RUNTIME_REQUEST_BYTES == MAX_HELPER_FRAME_BYTES
    assert (
        MAX_ARGV_BYTES
        == MAX_HOST_RUNTIME_REQUEST_BYTES - HOST_RUNTIME_REQUEST_ENVELOPE_BYTES
    )
    document = {
        "schema_version": 1,
        "action": "start",
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 1,
        "fence": "40000000-0000-4000-8000-000000000004",
        "arguments": [
            *(f"--mount=type=bind,src={index:05}" for index in range(6000)),
            "",
            "line one\nline two\r\n",
        ],
    }
    request = HostRuntimeRequest.model_validate(document)
    assert len(request.arguments) == 6002
    assert len(canonical_message(request)) < MAX_HOST_RUNTIME_REQUEST_BYTES


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


def test_runtime_reconciliation_identity_is_bound_to_cleanup_request_and_grant() -> None:
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
