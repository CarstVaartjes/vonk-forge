"""Db: connections."""

import sys
import time
from collections.abc import Callable
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import (
    DBAPIError,
    InterfaceError,
    OperationalError,
    TimeoutError,
)
from sqlalchemy.orm import Session, sessionmaker

from ..settings import DATABASE_WAIT_BUDGETS
from .constants import (
    _ALEMBIC_CONFIG,
    _DATABASE_RETRYABLE_ERRORS,
    _DATABASE_STARTUP_TIMEOUT_SECONDS,
)


class _DatabaseStartupContention(RuntimeError):
    """The current schema owner remains busy; retry after releasing ownership."""


def _database_sqlstate(error: BaseException) -> str | None:
    if not isinstance(error, DBAPIError):
        return None
    code = getattr(error.orig, "sqlstate", None)
    return code if isinstance(code, str) else None


def _startup_retryable(error: BaseException) -> bool:
    if isinstance(error, (_DatabaseStartupContention, TimeoutError)):
        return True
    if not isinstance(error, (InterfaceError, OperationalError)):
        return False
    code = _database_sqlstate(error)
    # Failed connection establishment may lack SQLSTATE even after a server
    # authentication refusal. Its cause remains unknown: retry is bounded and
    # never changes credentials or grants access.
    # Retry only a connection fault or PostgreSQL's cannot-connect-now startup
    # response. Permissions, schema damage and integrity refusals remain fatal.
    return code is None or code.startswith("08") or code == "57P03"


def postgresql_connect_args(*, component: str) -> dict[str, int | str]:
    """The shared connection and server budgets for every PostgreSQL engine."""
    budgets = DATABASE_WAIT_BUDGETS
    return {
        "connect_timeout": budgets.connect_timeout_seconds,
        "options": " ".join(
            (
                f"-c application_name=vonk:{component}",
                f"-c lock_timeout={budgets.lock_timeout_ms}",
                f"-c statement_timeout={budgets.statement_timeout_ms}",
                f"-c transaction_timeout={budgets.transaction_timeout_ms}",
                "-c idle_in_transaction_session_timeout="
                + f"{budgets.idle_in_transaction_timeout_ms}",
            )
        ),
    }


def build_engine(database_url: str, *, component: str = "control") -> Engine:
    """Build the one engine every component shares.

    The fixed wait budgets bound every wait; PostgreSQL receives all four
    server-side timeouts per connection, and its pool is explicitly bounded so
    a saturated pool fails within the pool timeout instead of queueing a
    connection forever. SQLite accepts neither the server options nor an
    explicit pool size, so it keeps the default pool.

    ``component`` becomes the connections' ``application_name`` (``vonk:<name>``),
    so ``pg_stat_activity`` names the Controller process behind any session; an
    admission transaction narrows it to the kind of work it is doing.
    """

    budgets = DATABASE_WAIT_BUDGETS
    # Every JSON column write is checked against its contract (stored_columns);
    # a document no contract describes is reported once, never refused.
    from ..stored_json import install_write_guard

    install_write_guard()
    if "postgres" in database_url:
        # transaction_timeout exists only on PostgreSQL 17+; the deployed
        # Compose service pins postgres:18.6@sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722, so the server accepts it.
        return create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=budgets.pool_size,
            max_overflow=budgets.max_overflow,
            pool_timeout=budgets.pool_timeout_seconds,
            connect_args=postgresql_connect_args(component=component),
        )
    engine = create_engine(database_url, pool_pre_ping=True, connect_args={})
    if engine.dialect.name == "sqlite":
        # The engine has not escaped to a service or worker. Adopt historical
        # owning projections on one exclusive checkout before any mapped read.
        from ..exact_integer_adoption import reconcile_exact_integer_schema

        try:
            reconcile_exact_integer_schema(engine)
        except BaseException:
            engine.dispose()
            raise
    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    from ..fleet_events import FleetEventRecorder

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
    """Connection failures and startup-owner contention share one deadline.

    Container DNS and PostgreSQL can become available in either order after a
    host or Docker restart.  Keep startup deterministic by retrying only
    typed connection faults, unknown connection establishment and exact startup
    contention. Diagnostics remain redacted and the fixed deadline never resets.
    Known schema and permission refusals remain fatal immediately; an untyped
    handshake failure remains unknown until the bounded attempt expires.
    The deadline controls retry admission; an individual connection attempt has
    its own per-host driver budget and can finish after that deadline.
    """
    if timeout_seconds < 0 or timeout_seconds > 900:
        raise ValueError("database startup timeout is outside the safe bound")
    deadline = monotonic() + timeout_seconds
    delay = 0.5
    attempts = 0
    while True:
        try:
            return operation()
        except (*_DATABASE_RETRYABLE_ERRORS, _DatabaseStartupContention) as error:
            if not _startup_retryable(error):
                raise
            remaining = deadline - monotonic()
            if remaining <= 0:
                if (
                    isinstance(error, (InterfaceError, OperationalError))
                    and _database_sqlstate(error) is None
                ):
                    print(
                        f"{label} startup deadline expired; connection establishment "
                        "cause unknown (driver SQLSTATE unavailable); credentials unchanged",
                        file=sys.stderr,
                        flush=True,
                    )
                raise
            wait = min(delay, remaining)
            attempts += 1
            if isinstance(error, _DatabaseStartupContention):
                reason = str(error)
            elif isinstance(error, (InterfaceError, OperationalError)):
                code = _database_sqlstate(error)
                reason = (
                    f"{type(error).__name__}; SQLSTATE {code}"
                    if code is not None
                    else "connection establishment cause unknown; driver SQLSTATE unavailable"
                )
            else:
                reason = type(error).__name__
            print(
                f"{label} unavailable during startup (attempt {attempts}; "
                f"{reason}); retrying in {wait:.1f}s",
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
    connection: Connection | None = None,
) -> None:
    """Apply the baseline revision, then let startup reconcile live metadata."""
    if not database_url.strip():
        raise RuntimeError("database URL secret is empty")
    config = Config(str(config_path))
    # ConfigParser treats percent signs as interpolation markers.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    if connection is not None:
        config.attributes["connection"] = connection
    command.upgrade(config, "head")
