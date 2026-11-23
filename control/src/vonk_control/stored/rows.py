"""Row reads and the JSON write guard, over the loaded column registry.

Importing this module loads every mapped table (:mod:`vonk_control.models`) and
every column binding (:mod:`vonk_control.stored.columns`), so the registry that
:func:`~vonk_control.stored.json_values.binding_for` reads is complete in every
process that reads rows or builds an engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event

from .. import (
    models as _models,  # noqa: F401 -- loads every mapped table onto Base.metadata
)
from ..catalog.revision_contract import cached_catalog_document
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..model_primitives import Base
from ..models.catalog import CatalogDocumentRevision
from ..strict_json import stored_document_detail
from . import columns as _columns  # noqa: F401 -- binds every JSON column on import
from .json_values import (
    _REGISTRY,
    JsonColumn,
    _discriminator,
    _guard_row,
    _row_subject,
    _row_table,
    binding_for,
    read_column,
    set_write_guard_mode,
)


def bindings() -> Mapping[str, JsonColumn]:
    """Every bound column, keyed ``table.column``."""

    return dict(_REGISTRY)


def read_row_column(row: object, column: str) -> Any | Residue | None:
    """:func:`read_column` for ``row.column``, with the row's own kind and id."""

    if isinstance(row, CatalogDocumentRevision) and column == "document":
        try:
            return cached_catalog_document(
                row.kind,
                row.content_digest,
                getattr(row, column),
            )
        except (TypeError, ValueError) as error:
            return retire_as_unknown(
                "catalog_document_revisions.document",
                _row_subject(row),
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                stored_document_detail(error) or type(error).__name__,
            )
    binding = binding_for(_row_table(row), column)
    return read_column(
        binding,
        getattr(row, column),
        kind=_discriminator(row, binding),
        subject=_row_subject(row),
    )


@dataclass
class _WriteGuardInstallation:
    installed: bool = False


_WRITE_GUARD = _WriteGuardInstallation()


def install_write_guard(*, strict: bool | None = None) -> None:
    """Check every ORM write of a JSON column against its contract.

    The Controller runs ``strict=False``: a document its contract refuses is
    reported once per column and shape and stored unchanged, because
    bookkeeping never blocks work (the reader returns a typed unknown for it).
    The test suite runs ``strict=True``, so a producer that writes a shape no
    contract describes fails where it writes. Without ``strict`` the mode is
    left as it is: every engine installs the guard, and a later engine must
    not relax a mode chosen once for the process.
    """

    if strict is not None:
        set_write_guard_mode(strict=strict)
    if _WRITE_GUARD.installed:
        return
    _WRITE_GUARD.installed = True
    event.listen(Base, "before_insert", _guard_row, propagate=True)
    event.listen(Base, "before_update", _guard_row, propagate=True)
