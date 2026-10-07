from __future__ import annotations

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import (
    EnrollmentBootstrapResponse,
    IssuedCertificateResponse,
    canonical_message,
)
from vonk_agent_protocol.enrollment import MAX_ENROLLMENT_RESPONSE_BYTES


@pytest.mark.parametrize("bootstrap", [True, False])
def test_response_budget_counts_complete_utf8_envelope_and_recovers(
    bootstrap: bool,
) -> None:
    model: type[EnrollmentBootstrapResponse | IssuedCertificateResponse]
    body: dict[str, object]
    if bootstrap:
        model = EnrollmentBootstrapResponse
        field = "ca_pem"
        body = {
            "controller_endpoint": "https://agents.example.test",
            "enrollment_endpoint": "https://enroll.example.test",
            "ca_fingerprint": "a" * 64,
            "ca_pem": "certificate",
            "controller_address": None,
            "service_hostnames": [],
            "host_helper_authority_public_key": "b" * 64,
        }
    else:
        model = IssuedCertificateResponse
        field = "certificate_pem"
        body = {
            "node_id": "spk_0123456789abcdef0123456789abcdef",
            "certificate_pem": "certificate",
            "chain_pem": "chain",
            "serial": "123",
            "fingerprint": "a" * 64,
            "not_before": "2026-10-07T00:00:00Z",
            "not_after": "2026-11-06T00:00:00Z",
            "generation": 1,
        }
    original = model.model_validate(body)
    remaining = MAX_ENROLLMENT_RESPONSE_BYTES - len(canonical_message(original))
    # These strings fit every declared character bound, but their real UTF-8
    # bytes and JSON escapes must be charged against the whole body.
    for padding in ("é" * remaining, "\n" * remaining):
        oversized = {**body, field: "certificate" + padding}
        with pytest.raises(ValidationError, match="enrollment response exceeds"):
            model.model_validate(oversized)
    boundary = {**body, field: "certificate" + "x" * remaining}
    accepted = model.model_validate(boundary)
    assert len(canonical_message(accepted)) == MAX_ENROLLMENT_RESPONSE_BYTES
    assert getattr(accepted, field) == boundary[field]
    with pytest.raises(ValidationError, match="enrollment response exceeds"):
        model.model_validate({**boundary, field: getattr(accepted, field) + "x"})
    assert canonical_message(model.model_validate(body)) == canonical_message(original)
