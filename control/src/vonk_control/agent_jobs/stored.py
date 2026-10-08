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
    """Validated bytes for exact receipt comparisons and digest verification.

    Controller recipe parents are stored in full but bind their canonical typed
    encoding. Agent orders and other accepted documents retain their bound wire
    bytes, including timestamp spellings. A damaged column has no valid bytes.
    """
    from ..job_documents import _RecipeParent
    from ..profile_stop_authority import ProfileJobRunStopJob

    value = read_row_column(row, column)
    if isinstance(value, Residue):
        return b""
    if isinstance(value, _RecipeParent | ProfileJobRunStopJob):
        return canonical_message(value)
    return canonical_message(getattr(row, column))
