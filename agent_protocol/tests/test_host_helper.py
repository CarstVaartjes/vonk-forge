from __future__ import annotations

import json
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    AgentProtocolError,
    RecipeRunObservationReceiptClaims,
    SignedRecipeRunObservationReceipt,
    canonical_message,
    host_artifact_signing_bytes,
    recipe_run_observation_receipt_signing_bytes,
)
from vonk_agent_protocol.host_helper import HostHelperSignature


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
