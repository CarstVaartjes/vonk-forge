"""Strict decoding for the current durable recipe-operation phase shape."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

type StoredPhaseItem = tuple[str, str, Mapping[str, object]]
type StoredPhases = tuple[tuple[StoredPhaseItem, ...], ...]


def decode_stored_phases(payload: Mapping[str, object]) -> StoredPhases | None:
    """Decode persisted child phases without defaulting or coercing fields.

    ``None`` means the stored phases do not read: their owner treats that as an
    unknown outcome and observes the operation again, rather than being told the
    request was refused.
    """

    if "phases" not in payload:
        return ()
    raw_phases = payload["phases"]
    if not isinstance(raw_phases, list) or not raw_phases:
        return None
    phases: list[tuple[StoredPhaseItem, ...]] = []
    seen_operations: set[str] = set()
    for raw_phase in raw_phases:
        if not isinstance(raw_phase, list) or not raw_phase:
            return None
        group: list[StoredPhaseItem] = []
        for raw_item in raw_phase:
            if not isinstance(raw_item, Mapping):
                return None
            operation_id = raw_item.get("operation_id")
            node_id = raw_item.get("node_id")
            item_payload = raw_item.get("payload")
            if (
                not isinstance(operation_id, str)
                or not isinstance(node_id, str)
                or not isinstance(item_payload, Mapping)
                or operation_id in seen_operations
            ):
                return None
            try:
                uuid.UUID(operation_id)
            except ValueError:
                return None
            seen_operations.add(operation_id)
            group.append((operation_id, node_id, dict(item_payload)))
        phases.append(tuple(group))
    return tuple(phases)
