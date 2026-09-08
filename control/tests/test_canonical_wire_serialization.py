from __future__ import annotations

import json
from datetime import UTC, datetime

from vonk_agent_protocol import canonical_message
from vonk_control.fleet_event_contract import NodeProfilePayload
from vonk_control.fleet_stream_contract import (
    FleetChangeEvent,
    FleetStreamEvent,
    NodeProfileChange,
)


def test_fleet_stream_root_retains_nested_canonical_model_policy() -> None:
    change = FleetChangeEvent(
        change=NodeProfileChange(
            entity_kind="node-profile",
            entity_id="spk_" + "a" * 32,
            node_id="spk_" + "a" * 32,
            occurred_at=datetime(2026, 9, 8, tzinfo=UTC),
            fields=NodeProfilePayload(
                node_id="spk_" + "a" * 32,
                profile_changed=True,
                display_name_changed=None,
            ),
        )
    )
    encoded = canonical_message(FleetStreamEvent(root=change))
    assert encoded == canonical_message(change)
    document = json.loads(encoded)
    assert document["schema_version"] == 1
    assert document["projection_refresh_required"] is True
    assert document["change"]["occurred_at"] == "2026-09-08T00:00:00Z"
    assert document["change"]["fields"] == {
        "schema_version": 1,
        "node_id": "spk_" + "a" * 32,
        "profile_changed": True,
    }
