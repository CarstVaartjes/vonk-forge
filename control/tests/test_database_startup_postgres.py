from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from pytest import MonkeyPatch
from sqlalchemy.engine import Connection, Engine
from vonk_control.db import initialize_database


def test_concurrent_fresh_startup_migrates_once(postgres_engine: Engine) -> None:
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    config_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    expected_migration_head = ScriptDirectory.from_config(
        Config(str(config_path))
    ).get_current_head()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda _: initialize_database(
                    database_url,
                    config_path=config_path,
                ),
                range(2),
            )
        )
    with postgres_engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
            == expected_migration_head
        )


def test_bookkeeping_adoption_failure_preserves_current_schema(
    postgres_engine: Engine,
    monkeypatch: MonkeyPatch,
) -> None:
    from vonk_control import db

    def broken_adoption(connection: Connection) -> None:
        # PostgreSQL aborts the current transaction after this fault; only a
        # real savepoint prevents it from undoing schema reconciliation.
        connection.exec_driver_sql("SELECT * FROM missing_adoption_bookkeeping")

    monkeypatch.setattr(db, "adopt_legacy_rows", broken_adoption)
    initialize_database(postgres_engine.url.render_as_string(hide_password=False))
    with postgres_engine.connect() as connection:
        db.verify_schema_is_current(connection)
        assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
