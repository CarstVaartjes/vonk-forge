"""``jobs.owner_kind``/``owner_id``: generated from the payload, so every writer agrees.

The database derives both from the payload, which makes a Job written by any
path (ORM, insert-or-replay, raw SQL) and a Job saved before the columns existed
read the same way, and an owner lookup an index lookup.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from vonk_control.db import reconcile_schema
from vonk_control.models import Base

pytestmark = pytest.mark.postgres

_INSERT = text(
    "INSERT INTO jobs (id, request_id, kind, state, actor, authority_revision, "
    "targets, payload_digest, payload, created_at, updated_at, version, "
    "current_attempt) VALUES (:id, :id, 'recipe.start', 'queued', 'a', 'r', '[]', "
    ":d, CAST(:p AS json), now(), now(), 1, 0)"
)


def _insert(connection, payload: str) -> str:
    job_id = str(uuid.uuid4())
    connection.execute(_INSERT, {"id": job_id, "d": "0" * 64, "p": payload})
    return job_id


def _owner(connection, job_id: str) -> tuple[str | None, str | None]:
    row = connection.execute(
        text("SELECT owner_kind, owner_id FROM jobs WHERE id = :id"), {"id": job_id}
    ).one()
    return row[0], row[1]


def test_owner_follows_the_payload_for_every_writer(postgres_engine):
    with postgres_engine.begin() as connection:
        Base.metadata.create_all(connection)
        job = _insert(connection, '{"owner_kind": "run", "owner_id": "r1"}')
        none = _insert(connection, "{}")
        assert _owner(connection, job) == ("run", "r1")
        assert _owner(connection, none) == (None, None)
        connection.execute(
            text("UPDATE jobs SET payload = CAST(:p AS json) WHERE id = :id"),
            {"id": job, "p": '{"owner_kind": "installation", "owner_id": "i1"}'},
        )
        assert _owner(connection, job) == ("installation", "i1")


def test_startup_adds_the_columns_to_jobs_written_before_them(postgres_engine):
    with postgres_engine.begin() as connection:
        Base.metadata.create_all(connection)
        connection.execute(text("DROP INDEX ix_jobs_owner"))
        connection.execute(text("ALTER TABLE jobs DROP COLUMN owner_kind"))
        connection.execute(text("ALTER TABLE jobs DROP COLUMN owner_id"))
        job = _insert(connection, '{"owner_kind": "run", "owner_id": "r1"}')
    with postgres_engine.begin() as connection:
        reconcile_schema(connection)
        assert _owner(connection, job) == ("run", "r1")
        indexes = connection.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'jobs'")
        ).scalars()
        assert "ix_jobs_owner" in set(indexes)
    with postgres_engine.begin() as connection:
        reconcile_schema(connection)  # a second startup changes nothing


@pytest.mark.parametrize(
    "predicate",
    ["owner_kind = 'run' AND owner_id = 'run-77'", "owner_id = 'run-77'"],
)
def test_an_owner_lookup_uses_the_index(postgres_engine, predicate):
    with postgres_engine.begin() as connection:
        Base.metadata.create_all(connection)
        connection.execute(
            text(
                "INSERT INTO jobs (id, request_id, kind, state, actor, "
                "authority_revision, targets, payload_digest, payload, created_at, "
                "updated_at, version, current_attempt) "
                "SELECT gen_random_uuid()::text, gen_random_uuid()::text, "
                "'recipe.start', 'succeeded', 'a', 'r', '[]', repeat('0', 64), "
                "json_build_object('owner_kind', 'run', 'owner_id', 'run-' || n), "
                "now(), now(), 1, 0 FROM generate_series(1, 20000) AS n"
            )
        )
        connection.execute(text("ANALYZE jobs"))
        plan = "\n".join(
            row[0]
            for row in connection.execute(
                text(f"EXPLAIN SELECT id FROM jobs WHERE {predicate}")
            )
        )
    assert "ix_jobs_owner" in plan, plan
