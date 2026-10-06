"""Every JSON column is bound to a contract model typed all the way down.

The registry (``vonk_control.stored_columns``) binds each ``JSON`` column of
``models.py`` to the Pydantic contract of the document it stores.  This guard
fails when

* a JSON column has no binding (``tools/json-column-baseline.json`` lists the
  columns still unbound; the list only falls and a stale entry fails),
* a binding names a column that does not exist,
* a bound contract contains an untyped level (``Any``, ``object``, ``JsonValue``,
  a bare ``dict``/``list`` or a mapping of those) that is not a declared
  ``ExternalPassthrough`` (the baseline's ``untyped`` list is likewise a
  ratchet), or
* the mechanism itself stops reading damaged documents as typed unknowns or
  stops refusing a write its contract does not describe.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models import Base
from vonk_control.stored_json import (
    ExternalPassthrough,
    bind,
    bindings,
    column_violations,
    dump_column,
    install_write_guard,
    json_columns,
    read_column,
    untyped_leaves,
    write_guard_mode,
)

BASELINE = Path(__file__).resolve().parents[2] / "tools/json-column-baseline.json"


def _baseline() -> dict[str, list[str]]:
    document = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    return {"unbound": document["unbound"], "untyped": document["untyped"]}


def test_every_json_column_is_bound_to_a_contract() -> None:
    columns = {f"{table}.{column}" for table, column in json_columns(Base)}
    bound = set(bindings())
    assert not bound - columns, (
        f"bound but not a JSON column: {sorted(bound - columns)}"
    )
    unbound = columns - bound
    listed = set(_baseline()["unbound"])
    assert unbound <= listed, (
        f"JSON column without a contract: {sorted(unbound - listed)}"
    )
    assert listed <= unbound, (
        f"stale entries in tools/json-column-baseline.json: {sorted(listed - unbound)}"
    )


def test_no_contract_contains_an_untyped_level() -> None:
    found = sorted(
        f"{path}: {why}"
        for binding in bindings().values()
        for path, why in column_violations(binding)
    )
    listed = set(_baseline()["untyped"])
    assert set(found) <= listed, (
        f"untyped contract levels: {sorted(set(found) - listed)}"
    )
    assert listed <= set(found), (
        f"stale untyped entries in tools/json-column-baseline.json: "
        f"{sorted(listed - set(found))}"
    )


def test_every_passthrough_states_its_reason() -> None:
    for declared in ExternalPassthrough.declared():
        assert declared.reason.strip()


Reasoned = Annotated[JsonValue, ExternalPassthrough("owned by the upstream registry")]


def test_walker_finds_each_kind_of_untyped_level() -> None:
    class Loose(BaseModel):
        anything: object
        mapping: dict[str, object]
        nested: list[dict[str, list[object]]]

    class Typed(BaseModel):
        model_config = ConfigDict(extra="forbid")
        labels: dict[str, str]
        upstream: Reasoned | None = None

    paths = {path for path, _why in untyped_leaves(Loose)}
    assert paths == {"Loose.anything", "Loose.mapping{}", "Loose.nested[]{}[]"}
    assert untyped_leaves(Typed) == []


def test_passthrough_without_a_reason_is_refused() -> None:
    with pytest.raises(ValueError):
        ExternalPassthrough("  ")


class _Marker(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ordinal: int


def _scratch_binding(**options: object):
    from vonk_control import stored_json

    key = "scratch.document"
    stored_json._REGISTRY.pop(key, None)
    try:
        return bind("scratch", "document", _Marker, **options)  # type: ignore[arg-type]
    finally:
        stored_json._REGISTRY.pop(key, None)


def test_reading_a_damaged_document_is_a_typed_unknown() -> None:
    binding = _scratch_binding()
    assert read_column(binding, {"ordinal": 3}, subject="row-1") == _Marker(ordinal=3)
    damaged = read_column(binding, {"ordinal": "three"}, subject="row-1")
    assert isinstance(damaged, Residue)
    assert damaged.kind == "scratch.document"
    assert damaged.subject == "row-1"
    assert "three" not in damaged.note
    assert isinstance(read_column(binding, None, subject="row-1"), Residue)
    assert isinstance(read_column(binding, [1], subject="row-1"), Residue)


def test_reading_adopts_a_document_that_carries_a_retired_field() -> None:
    binding = _scratch_binding()
    adopted = read_column(binding, {"ordinal": 3, "retired": True}, subject="row-1")
    assert adopted == _Marker(ordinal=3)


def test_a_nullable_column_reads_null_as_absence() -> None:
    binding = _scratch_binding(nullable=True)
    assert read_column(binding, None, subject="row-1") is None


def test_writing_checks_the_contract_and_dumps_the_model() -> None:
    binding = _scratch_binding()
    assert dump_column(binding, _Marker(ordinal=3)) == {"ordinal": 3}
    assert dump_column(binding, {"ordinal": 3}) == {"ordinal": 3}
    with pytest.raises(ValueError):
        dump_column(binding, {"ordinal": "three"})


def test_the_strict_write_guard_refuses_a_document_no_contract_describes() -> None:
    install_write_guard()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    from datetime import UTC, datetime

    from vonk_control.models import Job

    now = datetime(2026, 9, 1, tzinfo=UTC)
    row = Job(
        request_id="00000000-0000-4000-8000-000000000001",
        kind="probe",
        state="queued",
        actor="a",
        authority_revision="r",
        targets=[7],
        payload_digest="0" * 64,
        payload={},
        created_at=now,
        updated_at=now,
    )
    with Session(engine) as session, write_guard_mode(strict=True):
        session.add(row)
        with pytest.raises(ValueError, match=r"jobs\.targets"):
            session.flush()
