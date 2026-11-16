"""Db: startup."""

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import (
    SQLAlchemyError,
)

from .connections import (
    _database_sqlstate,
    _DatabaseStartupContention,
    build_engine,
    run_with_database_startup_retry,
    upgrade_schema,
)
from .constants import _ALEMBIC_CONFIG, _STARTUP_ADVISORY_LOCK
from .schema import reconcile_schema


def verify_schema_is_current(connection: Connection) -> None:
    """Reconcile the live catalog to current metadata within this transaction."""
    reconcile_schema(connection)


def initialize_database(
    database_url: str,
    *,
    config_path: Path = _ALEMBIC_CONFIG,
) -> None:
    """Serialize schema migration and reconciliation for API startup."""

    def initialize_once() -> None:
        engine = build_engine(database_url)
        try:
            if engine.dialect.name != "postgresql":
                raise RuntimeError(
                    "control database initialization requires PostgreSQL"
                )
            with engine.connect() as lock_connection:
                acquired = lock_connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": _STARTUP_ADVISORY_LOCK},
                ).scalar_one()
                lock_connection.commit()
                if acquired is not True:
                    raise _DatabaseStartupContention(
                        "Controller schema owner is busy; startup will retry"
                    )
                try:
                    # Authentication precedes schema ownership. Alembic and
                    # reconciliation reuse this connection; no new checkout or
                    # network establishment occurs while the owner is held.
                    with lock_connection.begin():
                        upgrade_schema(
                            database_url,
                            config_path=config_path,
                            connection=lock_connection,
                        )
                    try:
                        with lock_connection.begin():
                            verify_schema_is_current(lock_connection)
                    except SQLAlchemyError as error:
                        if _database_sqlstate(error) == "55P03":
                            raise _DatabaseStartupContention(
                                "Controller schema relation is busy; startup will retry"
                            ) from None
                        raise RuntimeError(
                            "Controller startup schema reconciliation failed and the "
                            "transaction was rolled back. This error class is not "
                            "retryable, so startup aborts; schema failure: "
                            f"{error}"
                        ) from error
                finally:
                    if lock_connection.in_transaction():
                        lock_connection.rollback()
                    lock_connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": _STARTUP_ADVISORY_LOCK},
                    )
                    lock_connection.commit()
        except SQLAlchemyError as error:
            if _database_sqlstate(error) == "55P03":
                raise _DatabaseStartupContention(
                    "Controller schema relation is busy; startup will retry"
                ) from None
            raise
        finally:
            engine.dispose()

    run_with_database_startup_retry(initialize_once, label="PostgreSQL")
