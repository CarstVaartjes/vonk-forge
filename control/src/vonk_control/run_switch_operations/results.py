"""Run/Switch stored-result reader at the ORM boundary."""

from __future__ import annotations

import json

from ..lifecycle.evidence import Residue, read_or_rebuild
from ..run_switch_contract import RunSwitchOperationResult
from ..strict_json import read_stored_document


def _stored_result(value: object) -> RunSwitchOperationResult | Residue | None:
    """Parse stored JSON strictly, including nested datetime and tuple fields.

    A damaged stored result is a :class:`Residue` (typed unknown), never an
    exception: a reader shows what it can, and the advancing tick retires the
    one operation (``_reject_invalid_operation``) while retaining the bytes.
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
