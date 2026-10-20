"""Run/Switch stored-result reader at the ORM boundary."""

from __future__ import annotations

import json
from typing import Any

from ..lifecycle.evidence import Residue, read_or_rebuild
from ..run_switch_contract import RunSwitchOperationResult
from ..strict_json import read_stored_document


def _stored_result(value: object) -> RunSwitchOperationResult | Residue | None:
    """Parse stored JSON strictly, including nested datetime and tuple fields.

    A damaged stored result is a :class:`Residue` (typed unknown), never an
    exception: a reader shows what it can, and the advancing tick re-observes
    accepted intent and exact child effects before resuming work.
    """

    if isinstance(value, RunSwitchOperationResult | Residue):
        return value
    if value is None:
        return None
    loaded = read_or_rebuild(
        kind="run-switch.result",
        subject="stored-result",
        read=lambda: read_stored_document(
            lambda document: RunSwitchOperationResult.model_validate_json(
                json.dumps(document), strict=True
            ),
            value,
        ),
    )
    return loaded


def _persisted_result(value: RunSwitchOperationResult) -> Any:
    """Validate mutable progress through its reader before crossing SQL JSON.

    A typed model may have been mutated or copied without validation. Dumping
    it alone is insufficient: nested measurements and cross-field invariants
    must survive the reader's JSON validation before they can be stored.
    """
    return RunSwitchOperationResult.model_validate_json(
        value.model_dump_json(), strict=True
    ).model_dump(mode="json")
