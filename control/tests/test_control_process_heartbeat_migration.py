from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_control_process_heartbeat_migration_preserves_existing_rows(
    tmp_path: Path,
) -> None:
    database = f"sqlite:///{tmp_path / 'heartbeats.sqlite'}"
    config = _config(database)
    engine = create_engine(database)
    command.upgrade(config, "0011_update_rollouts")
    existing_id = "11111111-1111-4111-8111-111111111111"
    now = "2026-08-06 10:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs "
                "(id, request_id, kind, state, actor, base_commit, targets, "
                "payload_digest, payload, current_attempt, created_at, updated_at) "
                "VALUES (:id, :request_id, 'probe', 'queued', 'admin', 'abc', "
                "'[]', :digest, '{}', 0, :now, :now)"
            ),
            {
                "id": existing_id,
                "request_id": "22222222-2222-4222-8222-222222222222",
                "digest": "a" * 64,
                "now": now,
            },
        )

    command.upgrade(config, "0012_control_process_heartbeats")

    columns = {
        column["name"]
        for column in inspect(engine).get_columns("control_process_heartbeats")
    }
    assert columns == {
        "id",
        "process_kind",
        "generation_id",
        "release_digest",
        "build_digest",
        "start_nonce",
        "loop_sequence",
        "completed_at",
    }
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT id FROM jobs WHERE id = :id"), {"id": existing_id}
        ).scalar_one() == existing_id
        connection.execute(
            text(
                "INSERT INTO control_process_heartbeats "
                "(id, process_kind, generation_id, release_digest, build_digest, "
                "start_nonce, loop_sequence, completed_at) VALUES "
                "(:id, 'worker', 'gen-a', :release, :build, :nonce, 1, :now)"
            ),
            {
                "id": "33333333-3333-4333-8333-333333333333",
                "release": f"sha256:{'b' * 64}",
                "build": f"sha256:{'c' * 64}",
                "nonce": "d" * 64,
                "now": now,
            },
        )

    command.downgrade(config, "0011_update_rollouts")

    assert "control_process_heartbeats" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT id FROM jobs WHERE id = :id"), {"id": existing_id}
        ).scalar_one() == existing_id
