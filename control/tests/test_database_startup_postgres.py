from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from pytest import MonkeyPatch
from sqlalchemy.engine import Connection, Engine
from vonk_control.db import initialize_database
from vonk_control.db import startup as startup_owner


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

    monkeypatch.setattr(startup_owner, "adopt_legacy_rows", broken_adoption)
    initialize_database(postgres_engine.url.render_as_string(hide_password=False))
    with postgres_engine.connect() as connection:
        db.verify_schema_is_current(connection)
        assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1


def test_contact_move_rolls_back_on_interruption_and_restarts(
    postgres_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """Catches dropping the old owner before a durable copy or skipping replay."""
    import pytest
    from sqlalchemy import event, insert, inspect
    from vonk_control.db import node_facts
    from vonk_control.models import AgentNode, Base

    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(
            insert(AgentNode),
            [
                {"node_id": "legacy", "state": "active"},
                {"node_id": "empty", "state": "active"},
            ],
        )
        connection.exec_driver_sql(
            "ALTER TABLE agent_nodes DROP COLUMN management_address"
        )
        connection.exec_driver_sql(
            "ALTER TABLE agent_nodes DROP COLUMN management_observed_at"
        )
        connection.exec_driver_sql("""CREATE TABLE agent_presence (
            node_id varchar(36) PRIMARY KEY, certificate_serial varchar(128),
            certificate_fingerprint varchar(128), management_address varchar(45),
            observed_at timestamptz)""")
        connection.exec_driver_sql("""INSERT INTO agent_presence VALUES
            ('legacy', 'old-certificate', 'derived-not-copied', '10.0.0.42',
             '2026-10-22T12:00:00Z')""")

    original_move = node_facts.move_contact_facts

    def interrupted_move(connection: Connection) -> None:
        def interrupt(_conn, _cursor, statement, _parameters, _context, _many):
            if statement == "DROP TABLE agent_presence":
                raise InterruptedError(
                    "Controller stopped after copy, before retirement"
                )

        event.listen(connection, "before_cursor_execute", interrupt)
        try:
            original_move(connection)
        finally:
            event.remove(connection, "before_cursor_execute", interrupt)

    url = postgres_engine.url.render_as_string(hide_password=False)
    with monkeypatch.context() as interruption:
        interruption.setattr(node_facts, "move_contact_facts", interrupted_move)
        with pytest.raises(InterruptedError):
            initialize_database(url)
    with postgres_engine.connect() as connection:
        assert "agent_presence" in inspect(connection).get_table_names()
        assert "management_address" not in {
            column["name"] for column in inspect(connection).get_columns("agent_nodes")
        }
    initialize_database(url)
    initialize_database(url)
    with postgres_engine.connect() as connection:
        assert "agent_presence" not in inspect(connection).get_table_names()
        assert connection.exec_driver_sql("""SELECT management_address,
            contact_certificate_serial FROM agent_nodes WHERE node_id='legacy'
        """).one() == ("10.0.0.42", "old-certificate")
        assert connection.exec_driver_sql("""SELECT management_address,
            management_observed_at FROM agent_nodes WHERE node_id='empty'
        """).one() == (None, None)
