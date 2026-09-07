from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

_DIGEST = "a" * 64
RECIPE_ROUTE_AUTHORITY_ID = str(
    uuid5(NAMESPACE_URL, "https://vonkforge.ai/local-recipes")
)
_LEGACY_RECONCILIATION_ID = "11111111-1111-4111-8111-111111111111"
_GRAPH_JOB_ID = "22222222-2222-4222-8222-222222222222"
_CURRENT_JOB_ID = "33333333-3333-4333-8333-333333333333"
_NOW = datetime(2026, 9, 7, tzinfo=UTC)


def _alembic_config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _upgrade(engine: Engine, revision: str) -> None:
    command.upgrade(
        _alembic_config(engine.url.render_as_string(hide_password=False)), revision
    )


def _seed_legacy_state(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reconciliations "
                "(id, authority_revision, status, summary, graph, graph_digest, "
                "created_at) VALUES (:id, 'authority', 'succeeded', '{}', '{}', "
                ":digest, :now)"
            ),
            [
                {"id": RECIPE_ROUTE_AUTHORITY_ID, "digest": _DIGEST, "now": _NOW},
                {"id": _LEGACY_RECONCILIATION_ID, "digest": _DIGEST, "now": _NOW},
            ],
        )
        connection.execute(
            text(
                "INSERT INTO route_publications "
                "(reconciliation_id, state, plan_digest, activation_marker, "
                "activation_marker_digest) VALUES (:id, 'completed', :digest, "
                "'{}', :digest)"
            ),
            [
                {"id": RECIPE_ROUTE_AUTHORITY_ID, "digest": _DIGEST},
                {"id": _LEGACY_RECONCILIATION_ID, "digest": _DIGEST},
            ],
        )
        connection.execute(
            text(
                "INSERT INTO route_publication_owner "
                "(singleton_id, reconciliation_id, owner_generation, updated_at) "
                "VALUES (1, :id, 7, :now)"
            ),
            {"id": RECIPE_ROUTE_AUTHORITY_ID, "now": _NOW},
        )
        connection.execute(
            text(
                "INSERT INTO jobs "
                "(id, request_id, kind, state, actor, authority_revision, targets, "
                "payload_digest, payload, current_attempt, created_at, updated_at, "
                "reconciliation_id) VALUES (:id, :request_id, :kind, 'queued', "
                "'operator', 'authority', '[]', :digest, '{}', 0, :now, :now, :rid)"
            ),
            [
                {
                    "id": _GRAPH_JOB_ID,
                    "request_id": "5" * 36,
                    "kind": "legacy-graph",
                    "digest": _DIGEST,
                    "now": _NOW,
                    "rid": _LEGACY_RECONCILIATION_ID,
                },
                {
                    "id": _CURRENT_JOB_ID,
                    "request_id": "6" * 36,
                    "kind": "recipe.start",
                    "digest": _DIGEST,
                    "now": _NOW,
                    "rid": None,
                },
            ],
        )
        connection.execute(
            text(
                "INSERT INTO reconciliation_operations "
                "(id, reconciliation_id, graph_operation_id, role, "
                "expected_payload_digest, state) VALUES "
                "('44444444-4444-4444-8444-444444444444', :rid, 'legacy-op', "
                "'primary', :digest, 'queued')"
            ),
            {"rid": _LEGACY_RECONCILIATION_ID, "digest": _DIGEST},
        )
        connection.execute(
            text(
                "INSERT INTO job_attempts "
                "(id, job_id, attempt, fence, worker_id, lease_deadline, state) "
                "VALUES (:id, :job_id, 1, :fence, 'worker', :deadline, 'running')"
            ),
            [
                {
                    "id": "77777777-7777-4777-8777-777777777777",
                    "job_id": _GRAPH_JOB_ID,
                    "fence": "88888888-8888-4888-8888-888888888888",
                    "deadline": _NOW + timedelta(minutes=5),
                },
                {
                    "id": "99999999-9999-4999-8999-999999999999",
                    "job_id": _CURRENT_JOB_ID,
                    "fence": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "deadline": _NOW + timedelta(minutes=5),
                },
            ],
        )
        connection.execute(
            text(
                "INSERT INTO job_log_entries "
                "(job_id, digest, content, created_at) VALUES "
                "(:job_id, :digest, 'legacy log', :now)"
            ),
            [
                {"job_id": _GRAPH_JOB_ID, "digest": "b" * 64, "now": _NOW},
                {"job_id": _CURRENT_JOB_ID, "digest": "c" * 64, "now": _NOW},
            ],
        )


def _assert_migrated_state(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {
        "reconciliations",
        "reconciliation_operations",
        "reconciliation_cancellations",
        "reconciliation_completion_generation",
    }.isdisjoint(tables)
    assert "recipe_route_authorities" in tables
    assert "reconciliation_id" not in {
        column["name"] for column in inspector.get_columns("jobs")
    }
    route_columns = {
        column["name"] for column in inspector.get_columns("route_publications")
    }
    assert "authority_id" in route_columns
    assert "reconciliation_id" not in route_columns
    owner_columns = {
        column["name"] for column in inspector.get_columns("route_publication_owner")
    }
    assert "authority_id" in owner_columns
    assert "reconciliation_id" not in owner_columns
    assert {
        (foreign_key["referred_table"], tuple(foreign_key["referred_columns"]))
        for foreign_key in inspector.get_foreign_keys("route_publications")
    } == {("recipe_route_authorities", ("authority_id",))}
    assert {
        (foreign_key["referred_table"], tuple(foreign_key["referred_columns"]))
        for foreign_key in inspector.get_foreign_keys("route_publication_owner")
    } == {("recipe_route_authorities", ("authority_id",))}

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT authority_id FROM recipe_route_authorities")
        ).scalar_one() == RECIPE_ROUTE_AUTHORITY_ID
        assert connection.execute(text("SELECT count(*) FROM route_publications")).scalar_one() == 0
        assert connection.execute(
            text("SELECT count(*) FROM route_publication_owner")
        ).scalar_one() == 0
        assert connection.execute(text("SELECT id FROM jobs")).fetchall() == [
            (_CURRENT_JOB_ID,)
        ]
        assert connection.execute(text("SELECT job_id FROM job_attempts")).fetchall() == [
            (_CURRENT_JOB_ID,)
        ]
        assert connection.execute(text("SELECT job_id FROM job_log_entries")).fetchall() == [
            (_CURRENT_JOB_ID,)
        ]


def test_sqlite_route_authority_migration_resets_legacy_state(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'route-authority.sqlite'}")
    _upgrade(engine, "0022_current_telemetry_defaults")
    _seed_legacy_state(engine)
    _upgrade(engine, "0023_recipe_route_authority")
    _assert_migrated_state(engine)


def test_postgres_route_authority_migration_resets_legacy_state(
    postgres_engine: Engine,
) -> None:
    _upgrade(postgres_engine, "0022_current_telemetry_defaults")
    _seed_legacy_state(postgres_engine)
    _upgrade(postgres_engine, "0023_recipe_route_authority")
    _assert_migrated_state(postgres_engine)
