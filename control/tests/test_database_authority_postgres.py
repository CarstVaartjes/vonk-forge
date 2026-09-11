from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Table
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_control.database_authority import (
    DatabaseAuthorityService,
)
from vonk_control.db import initialize_database
from vonk_control.models import (
    Base,
    ControlAuthorityHead,
    ControlAuthorityRevision,
)


@pytest.fixture
def authority(postgres_engine: Engine):
    tables = [
        table
        for table in (
            ControlAuthorityHead.__table__,
            ControlAuthorityRevision.__table__,
        )
        if isinstance(table, Table)
    ]
    Base.metadata.drop_all(postgres_engine, tables=tables)
    Base.metadata.create_all(postgres_engine, tables=list(reversed(tables)))
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    service = DatabaseAuthorityService(
        sessions, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )
    return service, sessions


def test_postgres_initialization_persists_revision_before_head(authority):
    service, sessions = authority

    revision = service.ensure_initialized()

    with sessions() as session:
        assert session.get(ControlAuthorityRevision, revision) is not None
        assert session.get(ControlAuthorityHead, 1).revision_id == revision


def test_concurrent_fresh_startup_migrates_once_and_creates_one_authority_head(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    config_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    expected_migration_head = ScriptDirectory.from_config(
        Config(str(config_path))
    ).get_current_head()

    with ThreadPoolExecutor(max_workers=2) as pool:
        revisions = list(
            pool.map(
                lambda _: initialize_database(
                    database_url,
                    config_path=config_path,
                ),
                range(2),
            )
        )
    assert revisions[0] == revisions[1]
    with postgres_engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
            == expected_migration_head
        )
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM control_authority_revisions"
            ).scalar_one()
            == 1
        )
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM control_authority_heads"
            ).scalar_one()
            == 1
        )
        assert (
            connection.exec_driver_sql(
                """
            SELECT count(*)
            FROM control_authority_heads AS head
            JOIN control_authority_revisions AS revision
              ON revision.revision_id = head.revision_id
            """
            ).scalar_one()
            == 1
        )
