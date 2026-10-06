"""The one binding of every JSON column to its contract model.

Every ``JSON`` column in :mod:`vonk_control.models` stores a document that has
one authoritative Pydantic contract.  This module owns the mechanism:

* :class:`JsonColumn` binds ``table.column`` to a contract (or, for a table
  that stores several document families, one contract per value of the row's
  discriminator column, typically ``kind``);
* :func:`read_column` is the one read: it validates the stored value through
  the contract, adopting a legacy row by dropping fields a newer contract
  retired, and returns a typed unknown (:class:`~.lifecycle.evidence.Residue`)
  for a value that cannot be read.  It never raises for damaged data;
* :func:`dump_column` is the one write: it validates the value against the
  column's contract and returns the JSON document to store;
* :func:`untyped_leaves` is the structural guard behind the registry test: a
  contract may not contain ``Any``, ``object``, ``JsonValue``, a bare
  ``dict``/``list`` or a mapping of those at any level, unless the level is a
  declared :class:`ExternalPassthrough`.

The bindings themselves live in :mod:`vonk_control.stored_columns`, which
imports the contract modules; this module imports none of them, so a contract
module can use :class:`ExternalPassthrough` without a cycle.
"""

from __future__ import annotations

import collections.abc as cabc
import json
import logging
import types
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel, JsonValue, RootModel, TypeAdapter
from sqlalchemy import JSON
from sqlalchemy.orm import DeclarativeBase

from .lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from .strict_json import read_stored_document, stored_document_detail

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExternalPassthrough:
    """Annotation marking a JSON level whose structure belongs to someone else.

    ``EngineArgument = Annotated[JsonValue, ExternalPassthrough("...")]`` keeps
    the value exactly as received while saying, in writing, whose it is and why
    the Controller may not type the inside (an upstream document, a third-party
    answer).  The reason is mandatory.  The registry guard treats an annotated
    level as a declared leaf and does not look inside it; everything else in a
    contract must be typed all the way down.  The reason is published in the
    JSON Schema as ``x-vonk-passthrough``.
    """

    reason: str

    _declared: ClassVar[list[ExternalPassthrough]] = []

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("an external passthrough must declare its reason")
        ExternalPassthrough._declared.append(self)

    @classmethod
    def declared(cls) -> tuple[ExternalPassthrough, ...]:
        return tuple(ExternalPassthrough._declared)

    def __get_pydantic_json_schema__(
        self, core_schema: Any, handler: Any
    ) -> dict[str, Any]:
        schema = dict(handler(core_schema))
        schema["x-vonk-passthrough"] = self.reason.strip()
        return schema


#: What a contract may name for one stored document: a model, a union or a list
#: of models, anything Pydantic can build a ``TypeAdapter`` for.
Contract = Any


@dataclass(frozen=True, slots=True)
class JsonColumn:
    """The contract of one ``JSON`` column.

    ``contracts`` maps a value of the row's ``discriminator`` attribute to the
    contract of the documents stored under it; a column with one contract uses
    the key ``None``.  ``nullable`` mirrors the column, so ``NULL`` is read as
    absence, not as damage.
    """

    table: str
    column: str
    contracts: Mapping[str | None, Contract]
    nullable: bool = False
    discriminator: str | None = None
    #: Contracts whose own fields are checked by the guard elsewhere (a
    #: package boundary such as ``vonk_forge_contracts``).
    note: str = ""
    _adapters: dict[str | None, TypeAdapter[Any]] = field(
        default_factory=dict, repr=False, compare=False, hash=False
    )

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}"

    def contract_for(self, kind: str | None) -> Contract | None:
        if kind in self.contracts:
            return self.contracts[kind]
        return self.contracts.get(None)

    def adapter_for(self, kind: str | None) -> TypeAdapter[Any] | None:
        contract = self.contract_for(kind)
        if contract is None:
            return None
        resolved = kind if kind in self.contracts else None
        adapter = self._adapters.get(resolved)
        if adapter is None:
            adapter = TypeAdapter(contract)
            self._adapters[resolved] = adapter
        return adapter

    def all_contracts(self) -> Iterator[tuple[str | None, Contract]]:
        yield from self.contracts.items()


_REGISTRY: dict[str, JsonColumn] = {}


def bind(
    table: str,
    column: str,
    contract: Contract | Mapping[str, Contract],
    *,
    nullable: bool = False,
    discriminator: str | None = None,
    note: str = "",
) -> JsonColumn:
    """Register the contract of ``table.column``; each column is bound once."""

    key = f"{table}.{column}"
    if key in _REGISTRY:
        raise ValueError(f"{key} is already bound to a contract")
    contracts: Mapping[str | None, Contract]
    if isinstance(contract, Mapping):
        if discriminator is None:
            raise ValueError(f"{key} binds one contract per kind and names no kind")
        contracts = {kind: item for kind, item in contract.items()}
    else:
        contracts = {None: contract}
    binding = JsonColumn(
        table=table,
        column=column,
        contracts=contracts,
        nullable=nullable,
        discriminator=discriminator,
        note=note,
    )
    _REGISTRY[key] = binding
    return binding


def bindings() -> Mapping[str, JsonColumn]:
    """Every bound column, keyed ``table.column``."""

    from . import stored_columns  # noqa: F401  (binds on import)

    return dict(_REGISTRY)


def binding_for(table: str, column: str) -> JsonColumn:
    found = bindings().get(f"{table}.{column}")
    if found is None:
        raise KeyError(f"{table}.{column} has no contract binding")
    return found


def json_columns(base: type[DeclarativeBase]) -> list[tuple[str, str]]:
    """Every ``JSON`` column of ``base``'s tables as ``(table, column)``."""

    found: list[tuple[str, str]] = []
    for table in base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSON):
                found.append((table.name, column.name))
    return sorted(found)


def _discriminator(row: object, binding: JsonColumn) -> str | None:
    if binding.discriminator is None:
        return None
    value = getattr(row, binding.discriminator, None)
    return value if isinstance(value, str) else None


def _row_table(row: object) -> str:
    return str(row.__class__.__table__.name)  # type: ignore[attr-defined]


def _row_subject(row: object) -> str:
    for name in ("id", "node_id", "request_key", "sha256"):
        value = getattr(row, name, None)
        if isinstance(value, str | int):
            return str(value)
    return "?"


def _validate(adapter: TypeAdapter[Any], raw: object) -> Any:
    # The JSON path keeps decoded database values under the same strict
    # semantics as bytes received on the wire.
    return read_stored_document(
        lambda document: adapter.validate_json(json.dumps(document, allow_nan=False)),
        raw,
    )


def read_column(
    binding: JsonColumn,
    raw: object,
    *,
    kind: str | None = None,
    subject: str = "?",
) -> Any | Residue | None:
    """Read a stored value through its contract; never raise for a damaged one.

    ``None`` is returned for a nullable column holding ``NULL``.  A value that
    does not validate, a kind the column has no contract for, or ``NULL`` in a
    required column is a typed unknown (:class:`Residue`) that names the
    column, the row and the failing field path, never the content.  A document
    that only carries fields a newer contract retired is adopted.
    """

    if raw is None:
        if binding.nullable:
            return None
        return retire_as_unknown(
            binding.key,
            subject,
            BookkeepingReason.ROW_INCOMPLETE,
            "required document is missing",
        )
    adapter = binding.adapter_for(kind)
    if adapter is None:
        return retire_as_unknown(
            binding.key,
            subject,
            BookkeepingReason.EVIDENCE_UNAVAILABLE,
            f"no contract for kind {kind!r}",
        )
    try:
        return _validate(adapter, raw)
    except (TypeError, ValueError, KeyError) as error:
        note = stored_document_detail(error) or type(error).__name__
    return retire_as_unknown(
        binding.key, subject, BookkeepingReason.PERSISTED_STATE_DAMAGED, note
    )


def read_row_column(row: object, column: str) -> Any | Residue | None:
    """:func:`read_column` for ``row.column``, with the row's own kind and id."""

    binding = binding_for(_row_table(row), column)
    return read_column(
        binding,
        getattr(row, column),
        kind=_discriminator(row, binding),
        subject=_row_subject(row),
    )


def dump_column(binding: JsonColumn, value: object, *, kind: str | None = None) -> Any:
    """The JSON document to store for ``value``: contract-checked, then dumped.

    A model (or list of models) is dumped with ``model_dump(mode="json")``; an
    already decoded document is validated through the contract first and stored
    as the contract's own dump, so a writer cannot persist a shape the reader
    would call damaged.  A value the contract refuses raises ``ValueError``:
    that is a producer defect, caught where it is written.
    """

    if value is None:
        if binding.nullable:
            return None
        raise ValueError(f"{binding.key} is required")
    adapter = binding.adapter_for(kind)
    if adapter is None:
        raise ValueError(f"{binding.key} has no contract for kind {kind!r}")
    if isinstance(value, BaseModel) or (
        isinstance(value, list) and any(isinstance(item, BaseModel) for item in value)
    ):
        typed = value
    else:
        typed = adapter.validate_json(json.dumps(value, allow_nan=False))
    return adapter.dump_python(typed, mode="json")


# --------------------------------------------------------------- write guard


_GUARD_MODE: Literal["warn", "strict"] = "warn"
_GUARD_INSTALLED = False
_REPORTED: set[tuple[str, str]] = set()


def install_write_guard(*, strict: bool = False) -> None:
    """Check every ORM write of a JSON column against its contract.

    The Controller runs ``strict=False``: a document its contract refuses is
    reported once per column and shape and stored unchanged, because
    bookkeeping never blocks work (the reader returns a typed unknown for it).
    The test suite runs ``strict=True``, so a producer that writes a shape no
    contract describes fails where it writes.
    """

    global _GUARD_INSTALLED
    from sqlalchemy import event

    from .models import Base

    set_write_guard_mode(strict=strict)
    if _GUARD_INSTALLED:
        return
    _GUARD_INSTALLED = True
    event.listen(Base, "before_insert", _guard_row, propagate=True)
    event.listen(Base, "before_update", _guard_row, propagate=True)


def set_write_guard_mode(*, strict: bool) -> None:
    global _GUARD_MODE
    _GUARD_MODE = "strict" if strict else "warn"


@contextmanager
def write_guard_mode(*, strict: bool) -> Iterator[None]:
    """Run a block under one guard mode (a test that writes damaged rows on purpose)."""

    previous = _GUARD_MODE
    set_write_guard_mode(strict=strict)
    try:
        yield
    finally:
        set_write_guard_mode(strict=previous == "strict")


def _guard_row(mapper: Any, connection: Any, row: object) -> None:
    from sqlalchemy import inspect

    state = inspect(row)
    assert state is not None
    table = mapper.local_table.name
    registry = bindings()
    for column in mapper.columns:
        if not isinstance(column.type, JSON):
            continue
        binding = registry.get(f"{table}.{column.key}")
        if binding is None:
            continue
        # An update re-checks only the columns it changes: validating an
        # unchanged multi-megabyte plan on every touch of its row is waste.
        if state.key is not None and not state.attrs[column.key].history.has_changes():
            continue
        value = getattr(row, column.key, None)
        if value is None and column.default is not None:
            # The column's own default fills a value the writer left unset;
            # the default is checked the way a written value is.
            value = _column_default(column)
        kind = _discriminator(row, binding)
        outcome = _check(binding, value, kind)
        if outcome is None:
            continue
        if _GUARD_MODE == "strict" and value in ({}, []):
            # A fixture's empty placeholder carries no document to check; the
            # Controller itself never writes one, and reports it below.
            continue
        if _GUARD_MODE == "strict":
            raise ValueError(f"{binding.key}: {outcome}")
        key = (binding.key, outcome)
        if key not in _REPORTED and len(_REPORTED) < 4096:
            _REPORTED.add(key)
            _LOG.warning(
                "json column written outside its contract",
                extra={"json_column": binding.key, "json_violation": outcome},
            )


def _column_default(column: Any) -> object:
    default = column.default
    if default is None or not getattr(default, "is_callable", False):
        return getattr(default, "arg", None)
    try:
        return default.arg(None)
    except TypeError:
        return default.arg()


def _check(binding: JsonColumn, value: object, kind: str | None) -> str | None:
    if value is None:
        return None if binding.nullable else "required document is missing"
    adapter = binding.adapter_for(kind)
    if adapter is None:
        return f"no contract for kind {kind!r}"
    try:
        adapter.validate_json(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as error:
        return stored_document_detail(error) or type(error).__name__
    return None


# ------------------------------------------------------------------- walker

_Violation = tuple[str, str]


def untyped_leaves(contract: Contract, *, name: str = "") -> list[_Violation]:
    """Every level of ``contract`` that is not typed all the way down.

    Returns ``(path, why)`` pairs.  ``Any``, ``object``, pydantic's
    ``JsonValue``, a bare ``dict``/``list``/``tuple``, a ``Mapping`` of those
    and a ``dict[...]`` whose value is one of those are untyped.  A field
    annotated with an :class:`ExternalPassthrough` subclass is a declared
    leaf.  Models defined in an external package (``vonk_forge_contracts``)
    are a package boundary, owned and guarded by that repository.
    """

    out: list[_Violation] = []
    _walk(contract, name or getattr(contract, "__name__", "contract"), set(), out)
    return out


def _walk(tp: Any, path: str, seen: set[Any], out: list[_Violation]) -> None:
    if tp is Any or tp is object:
        out.append((path, "Any/object"))
        return
    if tp is JsonValue:
        out.append((path, "JsonValue"))
        return
    origin = get_origin(tp)
    if origin is Annotated:
        if any(isinstance(item, ExternalPassthrough) for item in get_args(tp)[1:]):
            return
        _walk(get_args(tp)[0], path, seen, out)
        return
    if origin is Literal:
        return
    if origin in (Union, types.UnionType):
        for member in get_args(tp):
            _walk(member, path, seen, out)
        return
    if origin in (list, set, frozenset, cabc.Sequence, cabc.Set):
        for member in get_args(tp):
            _walk(member, f"{path}[]", seen, out)
        return
    if origin is tuple:
        for member in get_args(tp):
            if member is not Ellipsis:
                _walk(member, f"{path}[]", seen, out)
        return
    if origin in (dict, cabc.Mapping, cabc.MutableMapping):
        arguments = get_args(tp)
        if len(arguments) != 2:
            out.append((path, "untyped mapping"))
            return
        _walk(arguments[1], f"{path}{{}}", seen, out)
        return
    if tp in (dict, list, tuple, set, frozenset):
        out.append((path, f"bare {tp.__name__}"))
        return
    if isinstance(tp, TypeAdapter):
        _walk(tp._type, path, seen, out)  # type: ignore[attr-defined]
        return
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        if tp.__module__.split(".")[0] == "vonk_forge_contracts":
            return
        if tp in seen:
            return
        seen.add(tp)
        if issubclass(tp, RootModel):
            _walk(tp.model_fields["root"].annotation, f"{path}", seen, out)
            return
        for field_name, info in tp.model_fields.items():
            _walk(info.annotation, f"{tp.__name__}.{field_name}", seen, out)


def contract_models(binding: JsonColumn) -> list[type[BaseModel]]:
    """Every Pydantic model a column's contract names at its top level.

    A union, list or mapping contract contributes its member models; the
    models' own nested models come with their schemas.  Models of an external package.
    """

    found: dict[type[BaseModel], None] = {}

    def collect(tp: Any) -> None:
        origin = get_origin(tp)
        if origin is Annotated:
            collect(get_args(tp)[0])
        elif origin is not None:
            for member in get_args(tp):
                if member is not Ellipsis:
                    collect(member)
        elif (
            isinstance(tp, type)
            and issubclass(tp, BaseModel)
            and tp.__module__.split(".")[0] != "vonk_forge_contracts"
        ):
            found[tp] = None

    for _kind, contract in binding.all_contracts():
        collect(contract)
    return list(found)


def column_violations(binding: JsonColumn) -> list[_Violation]:
    """:func:`untyped_leaves` over every contract bound to ``binding``."""

    found: list[_Violation] = []
    for kind, contract in binding.all_contracts():
        label = binding.key if kind is None else f"{binding.key}[{kind}]"
        found.extend(untyped_leaves(contract, name=label))
    return found


__all__ = [
    "Contract",
    "ExternalPassthrough",
    "JsonColumn",
    "Residue",
    "bind",
    "binding_for",
    "bindings",
    "column_violations",
    "contract_models",
    "dump_column",
    "install_write_guard",
    "json_columns",
    "read_column",
    "read_row_column",
    "set_write_guard_mode",
    "untyped_leaves",
    "write_guard_mode",
]
