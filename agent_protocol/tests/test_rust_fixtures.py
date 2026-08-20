from __future__ import annotations

import hashlib
import json
from pathlib import Path

from vonk_agent_protocol import (
    AgentClaim,
    AgentResult,
    RecipeOperationRequest,
    SignedHostHelperGrant,
    canonical_message,
)

ROOT = Path(__file__).parents[1] / "fixtures"


def test_language_neutral_fixtures_are_canonical_and_manifest_bound() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert set(manifest) == {
        "enrollment-request.json",
        "host-helper-grant.json",
        "operation-poll.json",
        "operation-result.json",
    }
    for name, expected_digest in manifest.items():
        raw = (ROOT / name).read_bytes().rstrip(b"\n")
        document = json.loads(raw)
        assert canonical_message(document) == raw
        assert hashlib.sha256(raw).hexdigest() == expected_digest


def test_python_protocol_round_trips_every_shared_operation_fixture() -> None:
    claim_document = json.loads((ROOT / "operation-poll.json").read_text())
    claim = AgentClaim.parse(claim_document)
    request = RecipeOperationRequest.parse(claim.operation, claim.payload)
    result = AgentResult.parse(json.loads((ROOT / "operation-result.json").read_text()))
    helper = SignedHostHelperGrant.parse(
        json.loads((ROOT / "host-helper-grant.json").read_text())
    )

    assert json.loads(canonical_message(claim)) == claim_document
    assert request.operation.value == "recipe.install"
    assert json.loads(canonical_message(result)) == json.loads(
        (ROOT / "operation-result.json").read_text()
    )
    assert json.loads(canonical_message(helper.to_mapping())) == json.loads(
        (ROOT / "host-helper-grant.json").read_text()
    )
