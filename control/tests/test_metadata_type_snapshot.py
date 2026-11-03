from __future__ import annotations

from sqlalchemy import BigInteger, Column, Integer, MetaData, Table, Text

from .metadata_type_snapshot import drift, snapshot


def test_retyping_a_column_is_reported_through_any_alias() -> None:
    metadata = MetaData()
    Table("t", metadata, Column("n", Text), Column("m", Integer))
    before = snapshot(metadata)
    assert drift(metadata, before) == []
    table = metadata.tables["t"]  # an alias, as a name scanner would miss
    table.c["n"].type = BigInteger()
    assert drift(metadata, before) == ["t.n: Text -> BigInteger"]
