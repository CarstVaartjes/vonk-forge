"""Startup schema repair: contention retries, other failures stay logged skips."""

from unittest.mock import patch

import pytest
from psycopg.errors import DeadlockDetected, LockNotAvailable, UndefinedObject
from sqlalchemy import Column, Index, Integer, MetaData, Table, create_engine
from sqlalchemy.exc import DBAPIError
from vonk_control.db.schema import reconcile_schema


@pytest.mark.parametrize(
    ("cause", "retried"),
    [(LockNotAvailable, True), (DeadlockDetected, True), (UndefinedObject, False)],
)
def test_startup_retries_lock_timeouts_and_deadlocks_but_skips_other_errors(
    cause: type[Exception], retried: bool
) -> None:
    table = Table("t", MetaData(), Column("a", Integer))
    index = Index("ix_t_a", table.c.a)
    failure = DBAPIError("CREATE INDEX", {}, cause("boom"))
    with (
        create_engine("sqlite://").begin() as connection,
        patch("vonk_control.exact_integer_adoption.adopt_exact_integer_columns"),
        patch(
            "vonk_control.db.schema._schema_differences",
            return_value=[("add_index", index)],
        ),
        patch.object(Index, "create", side_effect=failure),
    ):
        if retried:
            with pytest.raises(DBAPIError):
                reconcile_schema(connection)
        else:
            reconcile_schema(connection)
