"""Db: startup."""

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import (
    SQLAlchemyError,
)
from sqlalchemy.orm import sessionmaker

from .connections import (
    _database_sqlstate,
    _DatabaseStartupContention,
    build_engine,
    run_with_database_startup_retry,
    upgrade_schema,
)
from .constants import _ALEMBIC_CONFIG, _LOGGER, _STARTUP_ADVISORY_LOCK
from .schema import reconcile_schema


def verify_schema_is_current(connection: Connection) -> None:
    """Reconcile the live catalog to current metadata within this transaction."""
    reconcile_schema(connection)


def adopt_legacy_rows(connection: Connection) -> None:
    """Move rows written before a model change onto their current encoding.

    Idempotent and bounded; it runs inside the startup advisory lock, after the
    schema is current.  Rows it misses are adopted lazily by their kind's
    lifecycle adapter, so a gap here heals instead of stranding a row.
    """
    from ..lifecycle.model_cache import adopt_legacy_operations

    adopted = adopt_legacy_operations(connection)
    if adopted:
        _LOGGER.info("Adopted %d cache operations onto the lifecycle schedule", adopted)
    from ..reference_facts import fill_reference_facts

    filled = fill_reference_facts(connection)
    if filled:
        _LOGGER.info("Filled %d stored reference facts", filled)


def strip_run_plan_alias(connection: Connection) -> int:
    """Remove the route name from every stored run plan; the rows changed.

    The route name is ``recipe_runs.alias`` alone.  Run plans written before
    that carry a second copy that the strict plan contract refuses.  The
    statement is idempotent and covers every row, history included, so no
    reader accepts the old shape.  Digests are never recomputed: the stored
    ``plan_digest`` is an admission token that the Sparks' container labels and
    stop payloads carry.

    Startup never fails on one row (that would leave no Controller API): a row
    that cannot be rewritten is logged and stays as it is (a busy row retries
    startup), and the typed
    unreadable-run path retains its workload and reports it.
    """
    stripped = 0
    for (run_id,) in connection.execute(
        # Candidates by text: no jsonb cast outside a row's savepoint, so a plan
        # that jsonb rejects cannot abort startup.
        text("SELECT id FROM recipe_runs WHERE plan::text LIKE '%\"alias\"%'")
    ).all():
        try:
            with connection.begin_nested():
                stripped += connection.execute(
                    text(
                        "UPDATE recipe_runs SET plan = (plan::jsonb - 'alias')::json "
                        "WHERE id = :id AND jsonb_exists(plan::jsonb, 'alias')"
                    ),
                    {"id": run_id},
                ).rowcount
        except SQLAlchemyError as error:
            if _database_sqlstate(error) == "55P03":
                raise _DatabaseStartupContention(
                    "A run row is busy; startup will retry"
                ) from None
            _LOGGER.exception(
                "Run %s keeps its stored route name; it is reported as unreadable",
                run_id,
            )
    return stripped


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
                            stripped = strip_run_plan_alias(lock_connection)
                            if stripped:
                                _LOGGER.info(
                                    "Removed the route name from %d stored run plans",
                                    stripped,
                                )
                            # Adoption is derived bookkeeping, not schema authority.
                            # A savepoint keeps a damaged historical row from undoing
                            # current schema reconciliation; readers heal rows lazily.
                            try:
                                with lock_connection.begin_nested():
                                    adopt_legacy_rows(lock_connection)
                            except SQLAlchemyError:
                                _LOGGER.exception(
                                    "Lifecycle bookkeeping adoption deferred; "
                                    "current schema remains available"
                                )
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
            # Schema authority is committed and released. Retained journals need
            # exact SQL child proof, owned by the converter's bounded row locks.
            # Normal worker passes continue remaining or temporarily locked rows.
            from ..fleet_profile_adapter_conversion import (
                convert_due_retained_applications,
            )

            converted = convert_due_retained_applications(
                sessionmaker(engine, expire_on_commit=False), datetime.now(UTC)
            )
            if converted:
                _LOGGER.info(
                    "Converted %d retained profile adapter journals", converted
                )
        except SQLAlchemyError as error:
            if _database_sqlstate(error) == "55P03":
                raise _DatabaseStartupContention(
                    "Controller schema relation is busy; startup will retry"
                ) from None
            raise
        finally:
            engine.dispose()

    run_with_database_startup_retry(initialize_once, label="PostgreSQL")
