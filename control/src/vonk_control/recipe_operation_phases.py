"""Strict decoding for the current durable recipe-operation phase shape."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

type StoredPhaseItem = tuple[str, str, Mapping[str, object]]
type StoredPhases = tuple[tuple[StoredPhaseItem, ...], ...]


def decode_stored_phases(payload: Mapping[str, object]) -> StoredPhases:
    """Decode persisted child phases without defaulting or coercing fields."""

    if "phases" not in payload:
        return ()
    raw_phases = payload["phases"]
    if not isinstance(raw_phases, list) or not raw_phases:
        raise ValueError("stored operation phases are invalid")
    phases: list[tuple[StoredPhaseItem, ...]] = []
    seen_operations: set[str] = set()
    for raw_phase in raw_phases:
        if not isinstance(raw_phase, list) or not raw_phase:
            raise ValueError("stored operation phases are invalid")
        group: list[StoredPhaseItem] = []
        for raw_item in raw_phase:
            if not isinstance(raw_item, Mapping):
                raise TypeError("stored operation phases are invalid")
            operation_id = raw_item.get("operation_id")
            node_id = raw_item.get("node_id")
            item_payload = raw_item.get("payload")
            if (
                not isinstance(operation_id, str)
                or not isinstance(node_id, str)
                or not isinstance(item_payload, Mapping)
                or operation_id in seen_operations
            ):
                raise ValueError("stored operation phases are invalid")
            try:
                uuid.UUID(operation_id)
            except ValueError as error:
                raise ValueError("stored operation phases are invalid") from error
            seen_operations.add(operation_id)
            group.append((operation_id, node_id, dict(item_payload)))
        phases.append(tuple(group))
    return tuple(phases)
