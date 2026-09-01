from __future__ import annotations

import pytest
from vonk_agent_protocol import (
    AgentProtocolError,
    RecipeRunObservationReceiptClaims,
    SignedRecipeRunObservationReceipt,
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
