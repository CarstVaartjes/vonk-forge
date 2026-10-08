"""Every JSON column is bound to a contract model typed all the way down.

The registry (``vonk_control.stored_columns``) binds each ``JSON`` column of
``models.py`` to the Pydantic contract of the document it stores.  This guard
fails when

* a JSON column has no binding,
* a binding names a column that does not exist,
* a bound contract contains an untyped level (``Any``, ``object``, ``JsonValue``,
  a bare ``dict``/``list`` or a mapping of those) that is not a declared
  ``ExternalPassthrough`` with a written reason, or
* the mechanism itself stops reading damaged documents as typed unknowns or
  stops refusing a write its contract does not describe.
"""

from __future__ import annotations

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


def test_every_json_column_is_bound_to_a_contract() -> None:
    columns = {f"{table}.{column}" for table, column in json_columns(Base)}
    bound = set(bindings())
    assert not bound - columns, (
        f"bound but not a JSON column: {sorted(bound - columns)}"
    )
    assert not columns - bound, (
        f"JSON column without a contract: {sorted(columns - bound)}"
    )


def test_no_contract_contains_an_untyped_level() -> None:
    found = sorted(
        f"{path}: {why}"
        for binding in bindings().values()
        for path, why in column_violations(binding)
    )
    assert found == [], f"untyped contract levels: {found}"


def test_every_passthrough_states_its_reason() -> None:
    declared = ExternalPassthrough.declared()
    assert declared
    for passthrough in declared:
        assert passthrough.reason.strip()


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


def _journal_documents():
    import hashlib
    from datetime import UTC, datetime

    from vonk_control.run_switch_journal_contract import (
        JournalRepairPurpose,
        NativeProgressWitness,
        RunSwitchJournalRepairEndEvidence,
        RunSwitchJournalRepairEvidence,
        RunSwitchJournalRepairPendingState,
    )

    now = datetime(2026, 10, 7, tzinfo=UTC)
    identity = "00000000-0000-4000-8000-000000000001"
    digest = hashlib.sha256(b"{}").hexdigest()
    return (
        RunSwitchJournalRepairPendingState(deadline_at=now, next_attempt_at=now),
        RunSwitchJournalRepairEndEvidence(
            code="run-switch.journal-repair-exhausted",
            operation_id=identity,
            request_key=identity,
            original_digest=digest,
            original_document="{}",
            recorded_at=now,
        ),
        RunSwitchJournalRepairEvidence(
            algorithm="zero-transfer-native-install-v1",
            purpose=JournalRepairPurpose.OWNER_OBSERVATION,
            operation_id=identity,
            request_key=identity,
            payload_digest="0" * 64,
            plan_digest="0" * 64,
            original_digest=digest,
            corrected_digest=digest,
            original_document="{}",
            native_samples=[
                NativeProgressWitness(
                    operation_id=identity,
                    attempt_id=identity,
                    node_id="spk_" + "0" * 32,
                    certificate_serial="serial",
                    fence=identity,
                    payload_digest="0" * 64,
                )
            ],
            recorded_at=now,
        ),
    )


@pytest.mark.parametrize("document", _journal_documents())
def test_journal_columns_return_models_and_keep_damaged_reads_non_blocking(document):
    """Catch raw-dict ORM reads, skipped decorated columns and refused damaged reads."""
    from datetime import UTC, datetime

    from sqlalchemy import text
    from sqlalchemy.exc import StatementError
    from vonk_control.models import (
        Job,
        RunSwitchJournalRepair,
        RunSwitchJournalRepairPending,
    )
    from vonk_control.run_switch_journal_contract import (
        RunSwitchJournalRepairEndEvidence,
        RunSwitchJournalRepairPendingState,
    )
    from vonk_control.stored_json import binding_for, read_row_column

    now = datetime(2026, 10, 7, tzinfo=UTC)
    identity = "00000000-0000-4000-8000-000000000001"
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    model = (
        RunSwitchJournalRepairPending
        if isinstance(document, RunSwitchJournalRepairPendingState)
        else RunSwitchJournalRepair
    )
    column = "progress" if model is RunSwitchJournalRepairPending else "evidence"
    table = Base.metadata.tables[model.__tablename__]
    # Decorated JSON must remain visible to the coverage and structural guards.
    assert (table.name, column) in json_columns(Base)
    assert column_violations(binding_for(table.name, column)) == []
    with Session(engine) as session, write_guard_mode(strict=True):
        session.add(
            Job(
                id=identity,
                request_id=identity,
                kind="probe",
                state="queued",
                actor="a",
                authority_revision="r",
                targets=[],
                payload_digest="0" * 64,
                payload={},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        if isinstance(document, RunSwitchJournalRepairPendingState):
            row = RunSwitchJournalRepairPending(
                job_id=identity, deadline_at=document.deadline_at, progress=document
            )
        else:
            row = RunSwitchJournalRepair(
                id=identity,
                job_id=identity,
                original_digest=document.original_digest,
                record_kind="end"
                if isinstance(document, RunSwitchJournalRepairEndEvidence)
                else "repair",
                evidence=document,
                created_at=now,
            )
        session.add(row)
        session.commit()
        session.expire_all()
        assert getattr(row, column) == document
        assert isinstance(getattr(row, column), type(document))
        assert read_row_column(row, column) == document
    # Core writes have no ORM event guard: the type itself must validate them.
    with (
        engine.begin() as connection,
        write_guard_mode(strict=True),
        pytest.raises(StatementError),
    ):
        connection.execute(table.update().values({column: {"wrong": True}}))
    # Fault injection bypasses the serializer, as a damaged database row would.
    with engine.begin() as connection:
        connection.execute(text(f"UPDATE {table.name} SET {column} = '{{}}'"))
    with Session(engine) as session:
        damaged = session.get(model, identity)
        assert damaged is not None
        outcome = getattr(damaged, column)
        assert isinstance(outcome, Residue)
        assert read_row_column(damaged, column) == outcome
    engine.dispose()
