from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.host_helper import (
    RestartVonkUnitOperation,
    SignedHostHelperGrant,
    SignedRecipeRunObservationReceipt,
    recipe_run_observation_receipt_signing_bytes,
)
from vonk_control.host_helper_authority import HostHelperGrantIssuer

FIXTURES = Path(__file__).parents[2] / "agent_protocol" / "fixtures"
PRIVATE_SEED = bytes([19]) * 32
PUBLIC_KEY = bytes.fromhex(
    "66cd608b928b88e50e0efeaa33faf1c43cefe07294b0b87e9fe0aba6a3cf7633"
)


def test_python_issuer_matches_the_rust_verified_host_grant_fixture() -> None:
    issuer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(PRIVATE_SEED),
        clock=lambda: datetime.fromtimestamp(2_100_000_000, UTC),
        request_id_factory=lambda: "10000000-0000-4000-8000-000000000001",
    )
    grant = issuer.issue_grant(
        node_id="spk_11111111111111111111111111111111",
        operation=RestartVonkUnitOperation(type="restart-vonk-unit", unit="agent"),
        expires_in_seconds=60,
    )
    raw = (FIXTURES / "host-helper-grant-python-issued.json").read_bytes().rstrip(b"\n")

    assert canonical_message(grant.to_mapping()) == raw
    parsed = SignedHostHelperGrant.parse(json.loads(raw))
    ed25519.Ed25519PublicKey.from_public_bytes(PUBLIC_KEY).verify(
        bytes.fromhex(parsed.signature.value),
        b"VONK-HOST-MAINTENANCE-HELPER-GRANT-V1\x00"
        + canonical_message(parsed.claims.to_mapping()),
    )


def test_rust_signed_receipt_fixture_is_a_controller_verifiable_wire_message() -> None:
    raw = (FIXTURES / "recipe-run-observation-receipt.json").read_bytes().rstrip(b"\n")
    receipt = SignedRecipeRunObservationReceipt.parse(json.loads(raw))

    assert canonical_message(receipt.to_mapping()) == raw
    assert receipt.claims.request_id == "10000000-0000-4000-8000-000000000001"
    ed25519.Ed25519PublicKey.from_public_bytes(PUBLIC_KEY).verify(
        bytes.fromhex(receipt.signature.value),
        recipe_run_observation_receipt_signing_bytes(receipt.claims),
    )
