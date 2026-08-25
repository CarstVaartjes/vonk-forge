"""Database engine and session construction."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_STARTUP_ADVISORY_LOCK = 8_241_779_103
_ALEMBIC_CONFIG = Path(__file__).resolve().parent / "alembic.ini"


def build_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    from .fleet_events import FleetEventRecorder

    sessions = sessionmaker(engine, expire_on_commit=False)
    FleetEventRecorder.install(sessions)
    return sessions


def upgrade_schema(
    database_url: str,
    *,
    config_path: Path = _ALEMBIC_CONFIG,
) -> None:
    """Upgrade the linear Alembic lineage to its maintained head."""
    if not database_url.strip():
        raise RuntimeError("database URL secret is empty")
    config = Config(str(config_path))
    # ConfigParser treats percent signs as interpolation markers.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def initialize_database(
    database_url: str,
    *,
    config_path: Path = _ALEMBIC_CONFIG,
) -> str:
    """Serialize schema migration and authority-head creation for API startup."""
    from .database_authority import DatabaseAuthorityService

    engine = build_engine(database_url)
    try:
        if engine.dialect.name != "postgresql":
            raise RuntimeError("control database initialization requires PostgreSQL")
        with engine.connect() as lock_connection:
            lock_connection.execute(
                text("SELECT pg_advisory_lock(:key)"),
                {"key": _STARTUP_ADVISORY_LOCK},
            )
            lock_connection.commit()
            try:
                upgrade_schema(database_url, config_path=config_path)
                authority = DatabaseAuthorityService(session_factory(engine))
                return authority.ensure_initialized(acquire_advisory_lock=False)
            finally:
                if lock_connection.in_transaction():
                    lock_connection.rollback()
                lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _STARTUP_ADVISORY_LOCK},
                )
                lock_connection.commit()
    finally:
        engine.dispose()
