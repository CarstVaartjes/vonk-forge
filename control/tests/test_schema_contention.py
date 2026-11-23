"""Startup schema repair: contention propagates so startup retries; other errors skip."""

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from psycopg.errors import DeadlockDetected, LockNotAvailable, UndefinedObject
from sqlalchemy import Column, Index, Integer, MetaData, Table, create_engine
from sqlalchemy.exc import DBAPIError

from vonk_control.db import startup
from vonk_control.db.connections import _DatabaseStartupContention
from vonk_control.db.schema import reconcile_schema

DB = Path(__file__).resolve().parents[1] / "src/vonk_control/db"


@pytest.mark.parametrize(
    ("cause", "propagates"),
    [(LockNotAvailable, True), (DeadlockDetected, True), (UndefinedObject, False)],
)
def test_reconcile_propagates_lock_timeouts_and_deadlocks_but_skips_other_errors(
    cause: type[Exception], propagates: bool
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
        if propagates:
            with pytest.raises(DBAPIError):
                reconcile_schema(connection)
        else:
            reconcile_schema(connection)


@pytest.mark.parametrize("cause", [LockNotAvailable, DeadlockDetected])
def test_startup_retries_contention_instead_of_aborting(
    cause: type[Exception],
) -> None:
    failure = DBAPIError("ALTER", {}, cause("boom"))
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    with (
        patch.object(startup, "verify_schema_is_current", side_effect=failure),
        patch.object(startup, "upgrade_schema"),
        patch.object(startup, "build_engine", return_value=engine),
        patch.object(
            startup,
            "run_with_database_startup_retry",
            side_effect=lambda attempt, **_: attempt(),
        ),
        pytest.raises(_DatabaseStartupContention),
    ):
        startup.initialize_database("postgresql://unused")


def test_every_schema_repair_catch_calls_the_contention_helper() -> None:
    uncovered = []
    for path in sorted(DB.glob("schema*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.ExceptHandler)
                and isinstance(node.type, ast.Name)
                and node.type.id == "SQLAlchemyError"
                and not any(
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_reraise_contention"
                    for statement in node.body
                    for call in ast.walk(statement)
                )
            ):
                uncovered.append(f"{path.name}:{node.lineno}")
    assert not uncovered
