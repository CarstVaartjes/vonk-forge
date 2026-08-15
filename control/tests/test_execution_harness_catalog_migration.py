import shutil
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from functools import cache
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

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
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
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


@pytest.fixture
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


def upgrade_database_to(connection: Connection, revision: str) -> None:
    command.upgrade(migration_config(str(connection.engine.url)), revision)


def scalar(connection: Connection, statement: str) -> object:
    return connection.scalar(text(statement))


def table_exists(connection: Connection, table_name: str) -> bool:
    return table_name in inspect(connection).get_table_names()


def column_exists(connection: Connection, table_name: str, column_name: str) -> bool:
    return column_name in {
        column["name"] for column in inspect(connection).get_columns(table_name)
    }


@cache
def cutover_migration_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/0027_execution_harness_catalog.py"
    )
    spec = spec_from_file_location("execution_harness_catalog_cutover", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_user_session_and_agent_operation_state(connection: Connection) -> None:
    now = "2026-08-15 00:00:00"
    connection.execute(
        text(
            "INSERT INTO users (id, subject, role, disabled_at, password_verifier) "
            "VALUES ('admin', 'admin@example.test', 'admin', NULL, NULL)"
        )
    )
    connection.execute(
        text(
            "INSERT INTO sessions (id, user_id, digest, expires_at, revoked_at) "
            "VALUES ('session', 'admin', :digest, :expires_at, NULL)"
        ),
        {"digest": "b" * 64, "expires_at": now},
    )
    connection.execute(
        text(
            "INSERT INTO jobs "
            "(id, request_id, kind, state, actor, base_commit, targets, "
            "payload_digest, payload, current_attempt, created_at, updated_at) "
            "VALUES ('job', 'request', 'reconcile', 'queued', 'admin', 'base', "
            "'{}', :digest, '{}', 0, :created_at, :updated_at)"
        ),
        {"digest": "c" * 64, "created_at": now, "updated_at": now},
    )
    connection.execute(
        text(
            "INSERT INTO agent_nodes (node_id, state, capabilities) "
            "VALUES ('node', 'active', '{}')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO agent_operations "
            "(id, parent_job_id, node_id, kind, payload_digest, payload, base_commit, "
            "state, current_attempt, created_at, updated_at) "
            "VALUES ('operation', 'job', 'node', 'reconcile', :digest, '{}', 'base', "
            "'queued', 0, :created_at, :updated_at)"
        ),
        {"digest": "d" * 64, "created_at": now, "updated_at": now},
    )


def assert_no_v1_ddl_has_run(connection: Connection) -> None:
    assert not table_exists(connection, "catalog_entities")
    assert not table_exists(connection, "catalog_entity_revisions")
    assert column_exists(connection, "cluster_mappings", "profile_name")
    assert not column_exists(connection, "cluster_mappings", "topology_name")


def test_fence_covers_every_0026_mutable_application_table(
    connection: Connection,
) -> None:
    upgrade_database_to(connection, "0026_telemetry_maintenance_state")
    migration = cutover_migration_module()
    seeded = set(getattr(migration, "_MIGRATION_OWNED_EMPTY_CHAIN_TABLES", ()))
    mutable = set(migration._mutable_application_state_tables(connection))

    assert seeded == {
        "alembic_version",
        "fleet_event_cursor",
        "reconciliation_completion_generation",
        "route_publication_owner",
        "telemetry_maintenance_state",
    }
    assert mutable == set(inspect(connection).get_table_names()) - seeded


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


def test_model_metadata_rejects_non_v1_catalog_revision_schema_version() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 15, tzinfo=UTC)
    with Session(engine) as session:
        entity = CatalogEntity(
            id="catalog-entity",
            kind="execution-harness",
            publisher="vonk-forge",
            slug="schema-version-test",
            title="Schema version test",
            created_by="test",
            created_at=now,
            updated_at=now,
        )
        session.add(entity)
        session.flush()
        session.add(
            CatalogEntityRevision(
                id="catalog-revision",
                entity_id=entity.id,
                revision_number=1,
                lifecycle="draft",
                schema_version=2,
                document={},
                content_sha256=None,
                created_by="test",
                created_at=now,
            )
        )

        with pytest.raises(IntegrityError):
            session.flush()


def test_fresh_migrated_database_rejects_non_v1_catalog_revision_schema_version(
    connection: Connection,
) -> None:
    upgrade_fresh_database_to_head(connection)
    now = "2026-08-15 00:00:00"
    connection.execute(
        text(
            "INSERT INTO catalog_entities "
            "(id, kind, publisher, slug, title, created_by, created_at, updated_at) "
            "VALUES (:id, :kind, :publisher, :slug, :title, :created_by, "
            ":created_at, :updated_at)"
        ),
        {
            "id": "catalog-entity",
            "kind": "execution-harness",
            "publisher": "vonk-forge",
            "slug": "migrated-schema-version-test",
            "title": "Migrated schema version test",
            "created_by": "test",
            "created_at": now,
            "updated_at": now,
        },
    )

    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                "INSERT INTO catalog_entity_revisions "
                "(id, entity_id, revision_number, lifecycle, schema_version, document, "
                "content_sha256, created_by, created_at) "
                "VALUES (:id, :entity_id, :revision_number, :lifecycle, "
                ":schema_version, :document, :content_sha256, :created_by, :created_at)"
            ),
            {
                "id": "catalog-revision",
                "entity_id": "catalog-entity",
                "revision_number": 1,
                "lifecycle": "draft",
                "schema_version": 2,
                "document": "{}",
                "content_sha256": None,
                "created_by": "test",
                "created_at": now,
            },
        )


def test_sqlite_fence_rejects_nonempty_prototype_mappings_before_v1_ddl(
    connection: Connection,
) -> None:
    upgrade_database_to(connection, "0026_telemetry_maintenance_state")
    connection.execute(
        text(
            "INSERT INTO cluster_mappings "
            "(id, recipe_revision_id, profile_name, generation, node_count, state, "
            "parameters, placement_digest, endpoint_owner_node_id, created_by, "
            "created_at, updated_at) "
            "VALUES (:id, :recipe_revision_id, :profile_name, :generation, "
            ":node_count, :state, :parameters, :placement_digest, "
            ":endpoint_owner_node_id, :created_by, :created_at, :updated_at)"
        ),
        {
            "id": "mapping",
            "recipe_revision_id": "prototype-recipe-revision",
            "profile_name": "prototype-profile",
            "generation": 1,
            "node_count": 1,
            "state": "planned",
            "parameters": "{}",
            "placement_digest": "a" * 64,
            "endpoint_owner_node_id": "prototype-node",
            "created_by": "test",
            "created_at": "2026-08-15 00:00:00",
            "updated_at": "2026-08-15 00:00:00",
        },
    )
    connection.commit()

    with pytest.raises(RuntimeError, match="cluster_mappings"):
        upgrade_fresh_database_to_head(connection)

    assert not table_exists(connection, "catalog_entities")
    assert column_exists(connection, "cluster_mappings", "profile_name")


def test_sqlite_fence_rejects_user_session_and_agent_operation_state_before_v1_ddl(
    connection: Connection,
) -> None:
    upgrade_database_to(connection, "0026_telemetry_maintenance_state")
    seed_user_session_and_agent_operation_state(connection)
    connection.commit()

    with pytest.raises(RuntimeError, match="agent_nodes"):
        upgrade_fresh_database_to_head(connection)

    assert_no_v1_ddl_has_run(connection)


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


def test_postgresql_fence_rejects_nonempty_prototype_recipe_state(
    postgres_engine: Engine,
) -> None:
    url = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(migration_config(url), "0026_telemetry_maintenance_state")
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO local_recipes "
                "(id, slug, title, description, source_kind, created_by, created_at, "
                "updated_at) "
                "VALUES ('prototype-recipe', 'prototype-recipe', 'Prototype recipe', "
                "'discarded state', 'local', 'test', '2026-08-15 00:00:00', "
                "'2026-08-15 00:00:00')"
            )
        )

    with pytest.raises(RuntimeError, match="local_recipes"):
        command.upgrade(migration_config(url), "head")

    with postgres_engine.connect() as connection:
        assert not table_exists(connection, "catalog_entities")
        assert column_exists(connection, "cluster_mappings", "profile_name")


def test_postgresql_fence_rejects_user_session_and_agent_operation_state_before_v1_ddl(
    postgres_engine: Engine,
) -> None:
    url = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(migration_config(url), "0026_telemetry_maintenance_state")
    with postgres_engine.begin() as connection:
        seed_user_session_and_agent_operation_state(connection)

    with pytest.raises(RuntimeError, match="agent_nodes"):
        command.upgrade(migration_config(url), "head")

    with postgres_engine.connect() as connection:
        assert_no_v1_ddl_has_run(connection)


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
