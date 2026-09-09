from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.helper_response import HostHelperResponse


def test_helper_rejection_keeps_required_nulls_and_omits_unused_diagnostics() -> None:
    value = {
        "schema_version": 1,
        "request_id": None,
        "status": "rejected",
        "evidence_sha256": None,
    }
    model = HostHelperResponse.model_validate(value)
    assert json.loads(canonical_message(model)) == value
    assert (
        HostHelperResponse.model_validate(
            value | {"exit_code": None, "error_code": None, "observation_receipt": None, "diagnostic": None}
        )
        == model
    )
    for field in ("request_id", "evidence_sha256"):
        missing = dict(value)
        del missing[field]
        with pytest.raises(ValidationError):
            HostHelperResponse.model_validate(missing)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "unknown"},
        {"request_id": "unbound-request"},
        {"evidence_sha256": "A" * 64},
        {"exit_code": True},
        {"exit_code": -1},
        {"exit_code": 256},
        {"error_code": "stderr: secret"},
        {"detail": "secret"},
    ],
)
def test_helper_response_rejects_untyped_or_unbounded_fields(changes: dict) -> None:
    with pytest.raises(ValidationError):
        HostHelperResponse.model_validate(
            {
                "schema_version": 1,
                "request_id": None,
                "status": "rejected",
                "evidence_sha256": None,
            }
            | changes
        )


def test_helper_runtime_response_carries_the_canonical_signed_receipt() -> None:
    receipt = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/recipe-run-observation-receipt.json"
        ).read_text()
    )
    response = HostHelperResponse.model_validate(
        {
            "schema_version": 1,
            "request_id": receipt["claims"]["request_id"],
            "status": "container-runtime-request-executed",
            "evidence_sha256": "a" * 64,
            "observation_receipt": receipt,
        }
    )
    assert response.observation_receipt is not None
    assert response.observation_receipt.to_mapping() == receipt
