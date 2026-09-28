import pytest
from vonk_agent_protocol.route_activation import ActivationMarker
from vonk_control.operation_api import _stored_activation_marker


def test_route_publication_reader_rejects_malformed_activation_marker() -> None:
    marker = ActivationMarker(
        schema_version=2,
        generation=1,
        state="published",
        authority_id="12345678-1234-5678-1234-567812345678",
        plan_digest="a" * 64,
        evidence_set_digest="b" * 64,
        routes_sha256="c" * 64,
        litellm_sha256="d" * 64,
        directory="00000001-" + "e" * 64,
        manifest_sha256="f" * 64,
    )
    assert _stored_activation_marker(marker.model_dump()) == marker
    with pytest.raises(RuntimeError, match="durable activation marker is invalid"):
        _stored_activation_marker(123)
