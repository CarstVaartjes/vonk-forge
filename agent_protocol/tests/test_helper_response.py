from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.helper_response import HostHelperResponse


def test_helper_rejection_keeps_required_nulls_and_omits_unused_diagnostics() -> None:
    value = {
        "schema_version": 1,
        "request_id": None,
        "status": "rejected",
    }
    model = HostHelperResponse.model_validate(value)
    assert json.loads(canonical_message(model)) == value
    assert (
        HostHelperResponse.model_validate(
            value
            | {
                "exit_code": None,
                "error_code": None,
                "process_running": None,
                "diagnostic": None,
            }
        )
        == model
    )
    missing = dict(value)
    del missing["request_id"]
    with pytest.raises(ValidationError):
        HostHelperResponse.model_validate(missing)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "unknown"},
        {"request_id": "unbound-request"},
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
            }
            | changes
        )
