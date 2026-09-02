"""Database engine, startup retry, and session construction."""

import sys
import time
from collections.abc import Callable
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError
from sqlalchemy.orm import Session, sessionmaker

_STARTUP_ADVISORY_LOCK = 8_241_779_103
_ALEMBIC_CONFIG = Path(__file__).resolve().parent / "alembic.ini"
_DATABASE_STARTUP_TIMEOUT_SECONDS = 120.0
_DATABASE_RETRYABLE_ERRORS = (InterfaceError, OperationalError, TimeoutError)


def build_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    from .fleet_events import FleetEventRecorder

    sessions = sessionmaker(engine, expire_on_commit=False)
    FleetEventRecorder.install(sessions)
    return sessions


def run_with_database_startup_retry[T](
    operation: Callable[[], T],
    *,
    timeout_seconds: float = _DATABASE_STARTUP_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    label: str = "database",
) -> T:
    """Retry transient database connection failures for a bounded interval.

    Container DNS and PostgreSQL can become available in either order after a
    host or Docker restart.  Keep startup deterministic by retrying only
    connection-class failures, logging a redacted diagnostic, and always
    re-raising once the fixed deadline expires.  Schema and permission errors
    remain fatal immediately.
    """
    if timeout_seconds < 0 or timeout_seconds > 900:
        raise ValueError("database startup timeout is outside the safe bound")
    deadline = monotonic() + timeout_seconds
    delay = 0.5
    attempts = 0
    while True:
        try:
            return operation()
        except _DATABASE_RETRYABLE_ERRORS as error:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise
            wait = min(delay, remaining)
            attempts += 1
            print(
                f"{label} unavailable during startup (attempt {attempts}; "
                f"{type(error).__name__}); retrying in {wait:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            sleep(wait)
            delay = min(delay * 2, 10.0)


def wait_for_database(database_url: str) -> None:
    """Wait for one bounded, authenticated PostgreSQL connection."""

    def connect_once() -> None:
        engine = build_engine(database_url)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            engine.dispose()

    run_with_database_startup_retry(connect_once, label="PostgreSQL")


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

    def initialize_once() -> str:
        engine = build_engine(database_url)
        try:
            if engine.dialect.name != "postgresql":
                raise RuntimeError(
                    "control database initialization requires PostgreSQL"
                )
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

    return run_with_database_startup_retry(initialize_once, label="PostgreSQL")
