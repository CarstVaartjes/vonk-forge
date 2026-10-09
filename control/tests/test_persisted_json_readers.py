"""Stored-reader damage cannot masquerade as accepted execution bytes."""

import json

import pytest
from vonk_agent_protocol import AgentOperation as OperationKind
from vonk_agent_protocol import GatewayRouteState, canonical_message
from vonk_agent_protocol.route_activation import ActivationMarker
from vonk_control.agent_jobs.stored import (
    column_field,
    column_is_document,
    column_message,
)
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models import AgentOperation
from vonk_control.operation_api import _stored_activation_marker

from .recipe_stop_fixtures import recipe_stop_payload as accepted_payload


@pytest.mark.parametrize("damage", [123, None, {}, {"generation": True}])
def test_route_publication_reader_cannot_invent_an_activation_and_accepts_repaired_json(
    damage,
) -> None:
    marker = ActivationMarker(
        schema_version=2,
        generation=1,
        state=GatewayRouteState.PUBLISHED.value,
        authority_id="12345678-1234-5678-1234-567812345678",
        plan_digest="a" * 64,
        evidence_set_digest="b" * 64,
        routes_sha256="c" * 64,
        litellm_sha256="d" * 64,
        directory="00000001-" + "e" * 64,
        manifest_sha256="f" * 64,
    )
    accepted = []
    with pytest.raises(Exception):  # noqa: B017 -- no invented activation; fresh repaired reader below
        accepted.append(_stored_activation_marker(damage))
    assert not accepted
    restored = _stored_activation_marker(json.loads(canonical_message(marker)))
    assert canonical_message(restored) == canonical_message(marker)


@pytest.mark.parametrize("damage", [None, [], {"port": True}, {"plan_digest": "bad"}])
def test_agent_queue_reader_damage_has_no_wire_bytes_and_repaired_order_is_readable(
    damage,
) -> None:
    """Catches scheduling from raw damaged payload fields or defaulting them."""
    row = AgentOperation(kind=OperationKind.RECIPE_STOP, payload=damage)
    assert isinstance(column_field(row, "payload", "plan_digest"), Residue)
    assert not column_is_document(row, "payload")
    assert column_message(row, "payload") == b""
    payload = accepted_payload("spk_" + "a" * 32, plan_digest="b" * 64)
    row.payload = json.loads(canonical_message(payload))
    assert column_is_document(row, "payload")
    assert column_field(row, "payload", "plan_digest") == "b" * 64
    assert column_message(row, "payload") == canonical_message(payload)
