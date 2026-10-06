"""Canonical, nonblocking SQL locks for resource admission transactions."""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session
from vonk_agent_protocol import AdmissionCode, UnknownOutcomeError

from .settings import DATABASE_WAIT_BUDGETS

_BUSY_SQLSTATES = frozenset({"55P03", "40P01", "40001", "57014"})
_ROW_LOCK_ORDER = (
    "agent_nodes",
    "catalog_document_revisions",
    "cluster_mappings",
    "recipe_builds",
    "recipe_installations",
    "cluster_mapping_nodes",
    "installation_nodes",
    "node_artifacts",
    "node_inventory_snapshots",
    "resource_reservations",
    "jobs",
    "agent_operations",
)
_ROW_LOCK_RANK = {table: rank for rank, table in enumerate(_ROW_LOCK_ORDER)}


class AdmissionLockBusy(UnknownOutcomeError, RuntimeError):
    """An admission lock could not be acquired within its bound.

    ``holder`` names the kind of work that holds the lock when PostgreSQL could
    say (see ``acquire_admission_keys``); it is ``None`` when it is unknown.
    ``sqlstate`` is the PostgreSQL condition that ended the attempt (``None``
    for a refused advisory try-lock) and ``activity`` summarises the other open
    transactions seen at that moment, so "busy" always says why and by whom.
    """

    code = AdmissionCode.CAPACITY_BUSY

    def __init__(
        self,
        message: str,
        *,
        holder: str | None = None,
        sqlstate: str | None = None,
        activity: str | None = None,
    ) -> None:
        super().__init__(message)
        self.holder = holder
        self.sqlstate = sqlstate
        self.activity = activity


#: An admission that has been refused this many times in a row stops trying and
#: starts queueing (bounded by ``patient_admission_lock_timeout_ms``).
PATIENT_AFTER_RETRIES = 3

_PATIENCE_MS: ContextVar[int] = ContextVar("vonk_admission_patience_ms", default=0)


@contextmanager
def patient_admission(refused_retries: int) -> Iterator[None]:
    """Let admission transactions in this context queue for their locks.

    A try-lock never queues, so a background writer that takes and releases a
    node's lock often enough can refuse every try forever.  After
    ``PATIENT_AFTER_RETRIES`` refusals the owning operation asks for patience:
    its next attempt waits, in PostgreSQL's FIFO lock queue and bounded by the
    patient lock timeout, and background writers (which only ever try) then fail
    on that node until it has been served.
    """

    if refused_retries < PATIENT_AFTER_RETRIES:
        yield
        return
    token = _PATIENCE_MS.set(DATABASE_WAIT_BUDGETS.patient_admission_lock_timeout_ms)
    try:
        yield
    finally:
        _PATIENCE_MS.reset(token)


def _patient() -> bool:
    return _PATIENCE_MS.get() > 0


def busy_detail(error: BaseException | None) -> str:
    """One sentence saying why an admission was refused and who held the way.

    Walks the exception chain for the lock failure (or the database error that
    carries a SQLSTATE).  A refusal that is not lock contention (a stale
    inventory, a mapping not yet ready, a port in use) names its own cause
    instead of being reported as a busy capacity writer.
    """

    seen: set[int] = set()
    cursor = error
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, AdmissionLockBusy):
            return f"Admission is waiting for the Controller capacity writer: {cursor}"
        if isinstance(cursor, DBAPIError):
            return (
                "Admission is waiting for the Controller capacity writer: "
                f"database refused the lock (SQLSTATE {_sqlstate(cursor)})"
            )
        cursor = cursor.__cause__ or cursor.__context__
    return (
        f"Admission is waiting: {error}"
        if error is not None
        else "Admission is waiting"
    )


@dataclass(frozen=True, slots=True, order=True)
class AdmissionLockKey:
    """One cooperative lock identity, sorted by namespace then exact identity."""

    namespace: str
    identity: str

    def __post_init__(self) -> None:
        if not self.namespace or not self.identity:
            raise ValueError("admission lock identity is incomplete")

    @property
    def database_key(self) -> str:
        return f"vonk-admission:{self.namespace}:{self.identity}"


@dataclass(frozen=True, slots=True)
class AdmissionRowLock:
    """A declared model-scoped row set for one admission transaction."""

    name: str
    model: type[Any]
    statement: Select[Any]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("admission row-lock name is empty")
        table = self.model.__tablename__
        if table not in _ROW_LOCK_RANK:
            raise ValueError(f"admission row-lock table is not ordered: {table}")


def node_admission_key(node_id: str) -> AdmissionLockKey:
    """Return the common cooperative lock identity for one target node."""

    return AdmissionLockKey("node", node_id)


def job_request_key(request_id: str) -> AdmissionLockKey:
    """Serialize creation for the unique ``jobs.request_id`` identity."""

    return AdmissionLockKey("job-request", request_id)


def is_admission_contention(error: DBAPIError) -> bool:
    """Whether PostgreSQL aborted an admission transaction for bounded wait."""

    original = error.orig
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return sqlstate in _BUSY_SQLSTATES


def acquire_admission_keys(
    session: Session,
    keys: Collection[AdmissionLockKey],
    *,
    holder: str | None = None,
) -> None:
    """Try every declared advisory key in canonical order, without waiting.

    ``holder`` names the kind of work taking the keys (for example
    ``"profile-admission"``). It labels this transaction's PostgreSQL session
    for as long as the transaction lives, which is how a taker that finds a key
    busy can name the work that holds it instead of only saying "busy".
    """

    if not keys:
        return
    if session.get_bind().dialect.name != "postgresql":
        return
    patient = _patient()
    _set_local_admission_timeout(session)
    if holder:
        _label_transaction(session, holder)
    function = "pg_advisory_xact_lock" if patient else "pg_try_advisory_xact_lock"
    statement = text(f"SELECT {function}(hashtextextended(:key, 0))")
    for key in sorted(set(keys)):
        try:
            with session.begin_nested():
                acquired = session.scalar(statement, {"key": key.database_key})
        except OperationalError as error:
            _raise_if_busy(session, error)
            raise
        if patient:
            continue  # the blocking form returns only once it holds the key
        if acquired is not True:
            owner = _key_holder(session, key)
            activity = None if owner else _activity(session)
            raise AdmissionLockBusy(
                f"admission lock {key.namespace} is busy"
                + (f" (held by {owner})" if owner else "")
                + (f"; other open transactions: {activity}" if activity else ""),
                holder=owner,
                activity=activity,
            )


def lock_admission_rows(
    session: Session, requests: Collection[AdmissionRowLock]
) -> Mapping[str, tuple[Any, ...]]:
    """Lock the complete declared row set by canonical table and primary key.

    The caller must determine and declare all row groups before calling this
    function. If a later re-read discovers another row that must be locked, the
    caller must roll back and replan rather than append an earlier lock.
    """

    names = [request.name for request in requests]
    if len(names) != len(set(names)):
        raise ValueError("admission row-lock names must be unique")
    postgres = session.get_bind().dialect.name == "postgresql"
    if postgres:
        _set_local_admission_timeout(session)
    locked: dict[str, tuple[Any, ...]] = {}
    ordered = sorted(
        requests,
        key=lambda request: (
            _ROW_LOCK_RANK[request.model.__tablename__],
            request.model.__tablename__,
            request.name,
        ),
    )
    for request in ordered:
        primary_key = tuple(request.model.__mapper__.primary_key)
        if not primary_key:
            raise ValueError("admission row-lock model has no primary key")
        statement = request.statement.order_by(None).order_by(*primary_key)
        if postgres:
            statement = statement.with_for_update(
                of=request.model, nowait=not _patient()
            )
        statement = statement.execution_options(populate_existing=True)
        try:
            with session.begin_nested():
                locked[request.name] = tuple(session.scalars(statement))
        except OperationalError as error:
            _raise_if_busy(session, error, table=request.model.__tablename__)
            raise
    return locked


_HOLDER_PREFIX = "vonk:"


def label_transaction(session: Session, holder: str) -> None:
    """Name this transaction's work in ``pg_stat_activity`` until it ends."""

    if session.get_bind().dialect.name != "postgresql":
        return
    _label_transaction(session, holder)


def _label_transaction(session: Session, holder: str) -> None:
    session.execute(
        text("SELECT set_config('application_name', :name, true)"),
        {"name": f"{_HOLDER_PREFIX}{holder}"[:63]},
    )


def _key_holder(session: Session, key: AdmissionLockKey) -> str | None:
    """The labelled work holding one advisory key, when PostgreSQL can say.

    Best effort and read only: an unlabelled holder (another process, an older
    release) or any failure answers ``None`` and never changes the refusal.
    """

    try:
        with session.begin_nested():
            name = session.scalar(
                text(
                    "SELECT a.application_name FROM pg_locks l "
                    "JOIN pg_stat_activity a ON a.pid = l.pid "
                    "WHERE l.locktype = 'advisory' AND l.granted "
                    "AND l.pid <> pg_backend_pid() AND l.objsubid = 1 "
                    "AND ((l.classid::bigint << 32) | l.objid::bigint) "
                    "= hashtextextended(:key, 0) "
                    "AND a.application_name LIKE :prefix LIMIT 1"
                ),
                {"key": key.database_key, "prefix": f"{_HOLDER_PREFIX}%"},
            )
    except DBAPIError:
        return None
    if isinstance(name, str) and name.startswith(_HOLDER_PREFIX):
        return name[len(_HOLDER_PREFIX) :] or None
    return None


def _set_local_admission_timeout(session: Session) -> None:
    """Apply the fixed admission bound to implicit index/FK waits."""

    timeout = _PATIENCE_MS.get() or DATABASE_WAIT_BUDGETS.admission_lock_timeout_ms
    session.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": f"{timeout}ms"},
    )


def _sqlstate(error: DBAPIError) -> str | None:
    original = error.orig
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)


def _activity(session: Session) -> str | None:
    """The other open transactions right now, oldest first (best effort).

    Row locks are not listed by PostgreSQL, so the transactions that could hold
    one are named instead: who they are (``application_name``), how old their
    transaction is and what they last ran.  Read only; any failure answers
    ``None`` and never changes the refusal.
    """

    try:
        with session.begin_nested():
            rows = session.execute(
                text(
                    "SELECT application_name, state, "
                    "EXTRACT(EPOCH FROM (now() - xact_start)), "
                    "left(regexp_replace(query, '\\s+', ' ', 'g'), 40) "
                    "FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND xact_start IS NOT NULL "
                    "AND backend_type = 'client backend' "
                    "ORDER BY xact_start LIMIT 4"
                )
            ).all()
    except DBAPIError:
        return None
    if not rows:
        return None
    return "; ".join(
        f"{name or 'unnamed'} {state} {float(age or 0):.1f}s [{query}]"
        for name, state, age, query in rows
    )


def _raise_if_busy(
    session: Session, error: OperationalError, *, table: str | None = None
) -> None:
    if not is_admission_contention(error):
        return
    sqlstate = _sqlstate(error)
    activity = _activity(session)
    raise AdmissionLockBusy(
        "admission lock is busy"
        + (f" on {table}" if table else "")
        + f" (SQLSTATE {sqlstate})"
        + (f"; other open transactions: {activity}" if activity else ""),
        sqlstate=sqlstate,
        activity=activity,
    ) from error


@dataclass(frozen=True, slots=True)
class AdmissionLockHolder:
    """One backend that holds an admission advisory lock right now."""

    node_id: str | None
    namespace: str
    holder: str
    state: str
    transaction_age_seconds: float
    query: str


@dataclass(frozen=True, slots=True)
class OpenTransaction:
    """One open database transaction, whether or not it holds an advisory key."""

    application_name: str
    state: str
    transaction_age_seconds: float
    query: str


@dataclass(frozen=True, slots=True)
class AdmissionLockReport:
    held: tuple[AdmissionLockHolder, ...]
    open_transactions: tuple[OpenTransaction, ...]


def report_admission_locks(session: Session) -> AdmissionLockReport:
    """Read only: the admission locks held now, who holds them, and for how long.

    Advisory keys are hashed, so each held lock is mapped back to a node by
    hashing every known node's key.  Row locks are not listed by PostgreSQL;
    ``open_transactions`` names the transactions that could hold one.
    """

    if session.get_bind().dialect.name != "postgresql":
        return AdmissionLockReport((), ())
    held = session.execute(
        text(
            "SELECT n.node_id, a.application_name, a.state, "
            "EXTRACT(EPOCH FROM (now() - a.xact_start)), "
            "left(regexp_replace(a.query, '\\s+', ' ', 'g'), 80) "
            "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
            "LEFT JOIN agent_nodes n ON ((l.classid::bigint << 32) | l.objid::bigint) "
            "= hashtextextended('vonk-admission:node:' || n.node_id, 0) "
            "WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1 "
            "AND a.datname = current_database() AND a.pid <> pg_backend_pid() "
            "ORDER BY a.xact_start"
        )
    ).all()
    open_rows = session.execute(
        text(
            "SELECT application_name, state, "
            "EXTRACT(EPOCH FROM (now() - xact_start)), "
            "left(regexp_replace(query, '\\s+', ' ', 'g'), 80) "
            "FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid() AND xact_start IS NOT NULL "
            "AND backend_type = 'client backend' ORDER BY xact_start LIMIT 50"
        )
    ).all()
    return AdmissionLockReport(
        tuple(
            AdmissionLockHolder(
                node_id,
                "node" if node_id else "other",
                (name or "unnamed").removeprefix(_HOLDER_PREFIX),
                state,
                float(age or 0),
                query,
            )
            for node_id, name, state, age, query in held
        ),
        tuple(
            OpenTransaction(
                (name or "unnamed").removeprefix(_HOLDER_PREFIX),
                state,
                float(age or 0),
                query,
            )
            for name, state, age, query in open_rows
        ),
    )
