import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import (
    CheckConstraint,
    Connection,
    Table,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from vonk_control.models import (
    Base,
    CatalogEntity,
    CatalogEntityRevision,
    ClusterMapping,
)


def migration_config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[Connection]:
    database_url = f"sqlite:///{tmp_path / 'execution-harness-catalog.sqlite'}"
    engine = create_engine(database_url)
    with engine.connect() as value:
        yield value


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[Engine]:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for PostgreSQL migration tests")
    try:
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "-e",
                "POSTGRES_PASSWORD=postgres",
                "-p",
                "127.0.0.1::5432",
                "postgres:16",
            ],
            text=True,
        ).strip()
    except subprocess.CalledProcessError as error:
        pytest.skip(f"disposable PostgreSQL is unavailable: {error}")
    try:
        port = subprocess.check_output(
            [
                "docker",
                "inspect",
                "-f",
                '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}',
                container,
            ],
            text=True,
        ).strip()
        engine = create_engine(
            f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/postgres"
        )
        for _ in range(100):
            try:
                with engine.connect():
                    break
            except (OSError, SQLAlchemyError):
                time.sleep(0.1)
        else:
            pytest.skip("disposable PostgreSQL did not become ready")
        yield engine
        engine.dispose()
    finally:
        subprocess.run(["docker", "stop", container], check=False, capture_output=True)


def upgrade_fresh_database_to_head(connection: Connection) -> None:
    command.upgrade(migration_config(str(connection.engine.url)), "head")


def scalar(connection: Connection, statement: str) -> object:
    return connection.scalar(text(statement))


def table_exists(connection: Connection, table_name: str) -> bool:
    return table_name in inspect(connection).get_table_names()


def column_exists(connection: Connection, table_name: str, column_name: str) -> bool:
    return column_name in {
        column["name"] for column in inspect(connection).get_columns(table_name)
    }


def test_fresh_database_reaches_the_v1_catalog_head(connection: Connection) -> None:
    upgrade_fresh_database_to_head(connection)
    assert scalar(connection, "select count(*) from local_recipe_revisions") == 0
    assert table_exists(connection, "catalog_entities")
    assert table_exists(connection, "catalog_entity_revisions")
    assert column_exists(connection, "cluster_mappings", "topology_name")
    assert not column_exists(connection, "cluster_mappings", "profile_name")


def test_fresh_database_catalog_and_topology_match_model_metadata(
    connection: Connection,
) -> None:
    upgrade_fresh_database_to_head(connection)
    inspector = inspect(connection)

    for model_table in (
        CatalogEntity.__table__,
        CatalogEntityRevision.__table__,
        ClusterMapping.__table__,
    ):
        assert [
            (
                column["name"],
                str(column["type"].compile(dialect=connection.dialect)),
                column["nullable"],
                bool(column["primary_key"]),
            )
            for column in inspector.get_columns(model_table.name)
        ] == [
            (
                column.name,
                str(column.type.compile(dialect=connection.dialect)),
                column.nullable,
                column.primary_key,
            )
            for column in model_table.columns
        ]
        assert _check_constraints(inspector, model_table) == _model_checks(
            model_table, connection
        )
        assert {
            (constraint["name"], tuple(constraint["column_names"]))
            for constraint in inspector.get_unique_constraints(model_table.name)
        } == {
            (constraint.name, tuple(constraint.columns.keys()))
            for constraint in model_table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        assert {
            (index["name"], tuple(index["column_names"]))
            for index in inspector.get_indexes(model_table.name)
        } == {
            (index.name, tuple(index.columns.keys())) for index in model_table.indexes
        }


def test_fresh_postgresql_database_reaches_the_v1_catalog_head(
    postgres_engine: Engine,
) -> None:
    command.upgrade(
        migration_config(postgres_engine.url.render_as_string(hide_password=False)),
        "head",
    )
    with postgres_engine.connect() as connection:
        assert scalar(connection, "select count(*) from local_recipe_revisions") == 0
        assert table_exists(connection, "catalog_entities")
        assert table_exists(connection, "catalog_entity_revisions")
        assert column_exists(connection, "cluster_mappings", "topology_name")
        assert not column_exists(connection, "cluster_mappings", "profile_name")
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )


def _check_constraints(inspector: object, model_table: Table) -> dict[str, str]:
    return {
        constraint["name"]: " ".join(constraint["sqltext"].split())
        for constraint in inspector.get_check_constraints(model_table.name)  # type: ignore[attr-defined]
    }


def _model_checks(model_table: Table, connection: Connection) -> dict[str, str]:
    return {
        constraint.name: " ".join(
            str(
                constraint.sqltext.compile(
                    dialect=connection.dialect,
                    compile_kwargs={"literal_binds": True},
                )
            ).split()
        )
        for constraint in model_table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }
