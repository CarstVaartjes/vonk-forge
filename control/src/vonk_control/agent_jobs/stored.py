"""Read heterogeneous queue documents through their canonical column contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel
from vonk_agent_protocol import canonical_message

from ..lifecycle.evidence import Residue
from ..stored_json import read_row_column


def column_value(row: object, column: str) -> Any:
    """Return the discriminator-selected model, explicit passthrough or residue."""
    return read_row_column(row, column)


def column_field(row: object, column: str, field: str) -> Any:
    """Project a scheduling fact from a validated heterogeneous document.

    Owned documents are models. Mapping access is only for the explicitly
    registered external/generic-job passthrough. Damage remains a typed value,
    rather than becoming an empty document or an invented default.
    """
    value = read_row_column(row, column)
    if isinstance(value, BaseModel):
        return getattr(value, field, None)
    if isinstance(value, Mapping):
        return value.get(field)
    return value if isinstance(value, Residue) else None


def column_is_document(row: object, column: str) -> bool:
    return isinstance(read_row_column(row, column), BaseModel | Mapping)


def column_message(row: object, column: str) -> bytes:
    """Canonical bytes for exact receipt comparisons and digest verification.

    A damaged column has no valid canonical bytes. The empty sentinel cannot
    match any accepted JSON document and is never stored or sent to an agent.
    """
    value = read_row_column(row, column)
    return b"" if isinstance(value, Residue) else canonical_message(value)
