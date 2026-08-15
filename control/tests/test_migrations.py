from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_legacy_migrations_round_trip_and_respect_authority_boundary(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'control.sqlite'}"
    config = _config(url)
    command.upgrade(config, "0026_telemetry_maintenance_state")
    engine = create_engine(url)
    names = set(inspect(engine).get_table_names())
    assert {
        "jobs", "job_attempts", "audit_events", "observations",
        "reconciliations", "users", "sessions",
    } <= names
    assert not ({"models", "profiles", "desired_profiles"} & names)

    command.downgrade(config, "base")
    assert "jobs" not in set(inspect(engine).get_table_names())
