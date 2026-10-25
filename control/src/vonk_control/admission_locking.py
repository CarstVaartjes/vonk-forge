"""Canonical, nonblocking SQL locks for resource admission transactions."""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from sqlalchemy import Index, PrimaryKeyConstraint, Select, UniqueConstraint, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AdmissionCode,
    Disposition,
    InvalidRequestError,
    InvalidRequestReason,
    UnknownOutcomeError,
    WaitReason,
)

from .bounded_retry import REQUEST_PAUSES, bounded_attempts
from .model_primitives import Base
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
    "node_inventory_snapshots",
    "resource_reservations",
    "fleet_profile_applications",
    "model_cache_operations",
    "model_cache_sets",
    "jobs",
    "agent_operations",
    "agent_operation_attempts",
)
_ROW_LOCK_RANK = {table: rank for rank, table in enumerate(_ROW_LOCK_ORDER)}
# Rows whose key never changes and which agent traffic references through
# foreign keys. They are locked FOR NO KEY UPDATE: that excludes every other
# updater but never the KEY SHARE a child row's foreign-key check takes (a
# child insert, or a child row written twice in one transaction), so contact,
# result, snapshot and telemetry writes cannot refuse admission.
_KEY_STABLE_TABLES = frozenset({"agent_nodes"})


class AdmissionLockBusy(UnknownOutcomeError, RuntimeError):
    """An admission lock could not be acquired within its bound.

    ``holder`` names the kind of work that holds the lock when PostgreSQL could
    say (see ``acquire_admission_keys``); it is ``None`` when it is unknown.
    ``sqlstate`` is the PostgreSQL condition that ended the attempt (``None``
    for a refused advisory try-lock). NOWAIT and an expired lock wait leave no
    blocking edge to inspect; their row-lock holder is unknown. Unrelated open
    transactions are never presented as evidence of a blocking owner.
    """

    #: A live owner or dependency holds the work: a wait, never a spent retry.
    disposition = Disposition.WAIT

    code = AdmissionCode.CAPACITY_BUSY

    def __init__(
        self,
        message: str,
        *,
        holder: str | None = None,
        sqlstate: str | None = None,
    ) -> None:
        super().__init__(message)
        self.holder = holder
        self.sqlstate = sqlstate


#: An admission that has been refused this many times in a row stops trying and
#: starts queueing (bounded by ``patient_admission_lock_timeout_ms``).
PATIENT_AFTER_RETRIES = 3


def admission_attempts() -> Iterator[int]:
    """The attempts of one request-led admission (see ``bounded_retry``).

    A try-lock refusal is contention that normally clears in milliseconds, so
    a request that queues work retries it ``PATIENT_AFTER_RETRIES`` times in all
    before it reports the refusal to its requester.
    """

    return bounded_attempts(REQUEST_PAUSES[: PATIENT_AFTER_RETRIES - 1])


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


class AdmissionLockNamespace(StrEnum):
    """Only node capacity has a cooperative admission lock identity."""

    NODE = "node"


@dataclass(frozen=True, slots=True, order=True)
class AdmissionLockKey:
    """One cooperative node-capacity lock, sorted by exact node identity."""

    identity: str
    namespace: AdmissionLockNamespace = field(
        init=False, default=AdmissionLockNamespace.NODE
    )

    def __post_init__(self) -> None:
        if not self.identity:
            raise ValueError("admission lock keys must name a nonempty node identity")

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

    return AdmissionLockKey(node_id)


def is_admission_contention(error: DBAPIError) -> bool:
    """Whether PostgreSQL aborted an admission transaction for bounded wait."""

    original = error.orig
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return sqlstate in _BUSY_SQLSTATES


class Conflict(StrEnum):
    """What a unique-constraint violation on an admission path means.

    A conflict re-plans, never loops: the same plan is never written twice.
    """

    #: An assumption of the plan was taken by another writer (a port, a slot,
    #: a number): decide again with the conflict as input.
    REPLAN = "replan"
    #: The request's own identity already has a record: that record answers.
    REPLAY = "replay"
    #: Anything else is a defect: fail at once, naming the constraint.
    DEFECT = "defect"


#: Every constraint whose violation an admission path decides, by the name
#: PostgreSQL reports.  Unnamed constraints carry PostgreSQL's default names
#: (``<table>_<columns>_key``, ``<table>_pkey``).  A name not listed is a DEFECT.
CONSTRAINTS: Mapping[str, Conflict] = MappingProxyType(
    {
        # Plan assumptions.
        "uq_promised_node_port": Conflict.REPLAN,
        "uq_active_node_port": Conflict.REPLAN,
        "recipe_library_sync_runs_active_slot_key": Conflict.REPLAN,
        "uq_fleet_profiles_number": Conflict.REPLAN,
        "agent_certificate_rotations_pkey": Conflict.REPLAN,
        # Request identities.
        "jobs_request_id_key": Conflict.REPLAY,
        "artifact_jobs_request_id_key": Conflict.REPLAY,
        "model_cache_operations_request_key_key": Conflict.REPLAY,
        "recipe_library_sync_runs_request_key_key": Conflict.REPLAY,
        "local_certificate_issuance_pkey": Conflict.REPLAY,
        "fleet_profile_applications_request_key_key": Conflict.REPLAY,
        "uq_distribution_plan_node": Conflict.REPLAY,
        # A reclaim racing a reclaim inserts the same successor id: the second
        # pass finds it active (same content) or refuses ASSIGNMENT_CONFLICT.
        "artifact_distribution_assignments_pkey": Conflict.REPLAY,
        "route_publication_owner_pkey": Conflict.REPLAY,
        "recipe_source_bundles_pkey": Conflict.REPLAY,
        "fleet_profiles_name_key": Conflict.REPLAY,
        "uq_catalog_document_identity": Conflict.REPLAY,
    }
)

_UNIQUE_VIOLATIONS = frozenset(
    {"23505", "SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY"}
)
_SQLITE_UNIQUE = re.compile(r"UNIQUE constraint failed: (.+)")
_SAFE_NAME = re.compile(r"[A-Za-z0-9_]{1,128}")
_SAFE_SQLSTATE = re.compile(r"[0-9A-Z_]{5,40}")
_UNKNOWN_CONSTRAINT = "unknown-constraint"


class ConstraintReplan(UnknownOutcomeError):
    """A plan assumption was taken by another writer: plan again with it."""

    def __init__(
        self,
        message: str | None = None,
        *,
        constraint: str,
        sqlstate: str | None = None,
    ) -> None:
        super().__init__(
            message
            or f"Database constraint {constraint} was taken by another writer; "
            "the request is planned again.",
            reason=WaitReason.SCOPE_CHANGED,
        )
        self.constraint = constraint
        self.sqlstate = sqlstate


class ConstraintDefect(InvalidRequestError):
    """An unregistered constraint refused the write: fail at once, with evidence."""

    def __init__(self, constraint: str, sqlstate: str | None) -> None:
        super().__init__(
            f"Database constraint {constraint} refused the write "
            f"(SQLSTATE {sqlstate or 'not reported'}); the request is not retried.",
            reason=InvalidRequestReason.CONFLICT,
        )
        self.constraint = constraint
        self.sqlstate = sqlstate


class ConstraintReplay(InvalidRequestError):
    """The request's identity already has a record; ``replay`` returns it."""

    def __init__(self, constraint: str, sqlstate: str | None) -> None:
        super().__init__(
            f"Database constraint {constraint} already holds this request.",
            reason=InvalidRequestReason.DUPLICATE,
        )
        self.constraint = constraint
        self.sqlstate = sqlstate

    def replay[R](self, lookup: Callable[[], R | None]) -> R:
        """The record that answers the request, or a DEFECT when there is none."""

        found = lookup()
        if found is None:
            raise ConstraintDefect(self.constraint, self.sqlstate) from None
        return found


def constraint_conflict(error: IntegrityError) -> tuple[Conflict, str]:
    """Decide a violation by its constraint name; an unknown name is a DEFECT."""

    conflict, constraint, _ = _decided(error)
    return conflict, constraint


@contextmanager
def admitting() -> Iterator[None]:
    """Turn a constraint violation inside this block into its typed conflict.

    Enter it outside the transaction (``with admitting(), sessions.begin()``)
    so a violation met at commit is decided too.  REPLAN raises
    ``ConstraintReplan``, REPLAY ``ConstraintReplay`` (whose ``replay`` returns
    the existing record) and DEFECT ``ConstraintDefect``.  The evidence is the
    constraint name and SQLSTATE, never SQL text or parameters.
    """

    try:
        yield
    except IntegrityError as error:
        conflict, constraint, sqlstate = _decided(error)
        if conflict is Conflict.REPLAN:
            raise ConstraintReplan(constraint=constraint, sqlstate=sqlstate) from None
        if conflict is Conflict.REPLAY:
            raise ConstraintReplay(constraint, sqlstate) from None
        raise ConstraintDefect(constraint, sqlstate) from None


def _decided(error: IntegrityError) -> tuple[Conflict, str, str | None]:
    """The decision, with its evidence: constraint name and SQLSTATE.

    Only a unique-key violation can be a REPLAN or a REPLAY; a check, foreign
    key or not-null violation is a DEFECT whatever its name.
    """

    original = error.orig
    sqlstate = (
        getattr(original, "sqlstate", None)
        or getattr(original, "pgcode", None)
        or getattr(original, "sqlite_errorname", None)
    )
    diagnostic = getattr(original, "diag", None)
    name = getattr(diagnostic, "constraint_name", None)
    sqlite = _SQLITE_UNIQUE.match(str(original))
    if name is None and sqlite is not None:
        name = _sqlite_constraint(sqlite.group(1))
    constraint = (
        name
        if isinstance(name, str) and _SAFE_NAME.fullmatch(name)
        else _UNKNOWN_CONSTRAINT
    )
    unique = sqlstate in _UNIQUE_VIOLATIONS or sqlite is not None
    return (
        CONSTRAINTS.get(constraint, Conflict.DEFECT) if unique else Conflict.DEFECT,
        constraint,
        sqlstate
        if isinstance(sqlstate, str) and _SAFE_SQLSTATE.fullmatch(sqlstate)
        else None,
    )


def _sqlite_constraint(reported: str) -> str | None:
    """PostgreSQL's name for the unique key SQLite reports by its columns."""

    qualified = [part.strip() for part in reported.split(",")]
    tables = {part.partition(".")[0] for part in qualified}
    if len(tables) != 1:
        return None
    (table_name,) = tables
    columns = tuple(part.partition(".")[2] for part in qualified)
    table = Base.metadata.tables.get(table_name)
    if table is None:
        return None
    names = sorted(
        _default_name(table_name, key)
        for key in (*table.constraints, *table.indexes)
        if isinstance(key, PrimaryKeyConstraint | UniqueConstraint)
        or (isinstance(key, Index) and key.unique)
        if tuple(column.name for column in key.columns) == columns
    )
    if not names:
        return None
    # Partial indexes over the same columns: decided alike, or not at all.
    if len({CONSTRAINTS.get(name, Conflict.DEFECT) for name in names}) != 1:
        return None
    return names[0]


def _default_name(
    table: str, key: PrimaryKeyConstraint | UniqueConstraint | Index
) -> str:
    if isinstance(key.name, str):
        return key.name
    if isinstance(key, PrimaryKeyConstraint):
        return f"{table}_pkey"
    return "_".join((table, *(column.name for column in key.columns), "key"))


def admission_wait_exhausted(error: BaseException) -> bool:
    """Whether this request already spent a PostgreSQL wait budget.

    Implicit index/FK lock timeouts and statement timeouts have already waited;
    repeating them in the request multiplies the admission latency. Immediate
    advisory/NOWAIT refusals are normalized as AdmissionLockBusy and can still
    retry. Patient admission waits have their own explicit finite budget.
    """

    seen: set[int] = set()
    cursor: BaseException | None = error
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, AdmissionLockBusy):
            return cursor.sqlstate == "57014" or (
                _patient() and cursor.sqlstate == "55P03"
            )
        if isinstance(cursor, DBAPIError):
            return _sqlstate(cursor) in {"55P03", "57014"}
        cursor = cursor.__cause__ or cursor.__context__
    return False


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
    set_admission_timeout(session)
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
            raise AdmissionLockBusy(
                f"admission lock {key.namespace} is busy"
                + (f" (held by {owner})" if owner else "; blocking pid unavailable"),
                holder=owner,
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
        set_admission_timeout(session)
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
                of=request.model,
                nowait=not _patient(),
                key_share=request.model.__tablename__ in _KEY_STABLE_TABLES,
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

    Best effort and read only: include unlabelled owners by PID. A failed
    observation answers ``None`` and never changes the refusal.
    """

    try:
        with session.begin_nested():
            row = session.execute(
                text(
                    "SELECT a.pid, a.application_name FROM pg_locks l "
                    "JOIN pg_stat_activity a ON a.pid = l.pid "
                    "WHERE l.locktype = 'advisory' AND l.granted "
                    "AND l.pid <> pg_backend_pid() AND l.objsubid = 1 "
                    "AND ((l.classid::bigint << 32) | l.objid::bigint) "
                    "= hashtextextended(:key, 0) "
                    "LIMIT 1"
                ).bindparams(key=key.database_key)
            ).one_or_none()
    except DBAPIError:
        return None
    if row is None:
        return None
    pid, name = row
    return f"pid {pid} {(name or 'unnamed').removeprefix(_HOLDER_PREFIX)}"


def set_admission_timeout(session: Session) -> None:
    """Apply the fixed admission bound to implicit index/FK waits."""

    if session.get_bind().dialect.name != "postgresql":
        return
    timeout = _PATIENCE_MS.get() or DATABASE_WAIT_BUDGETS.admission_lock_timeout_ms
    session.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": f"{timeout}ms"},
    )


def _sqlstate(error: DBAPIError) -> str | None:
    original = error.orig
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)


def _raise_if_busy(
    session: Session, error: OperationalError, *, table: str | None = None
) -> None:
    if not is_admission_contention(error):
        return
    sqlstate = _sqlstate(error)
    # NOWAIT never queues, and a timeout has already removed its wait edge.
    # pg_blocking_pids cannot identify that former holder; a bystander's last
    # SELECT is no evidence of which transaction retained the row lock.
    raise AdmissionLockBusy(
        "admission lock is busy"
        + (f" on {table}" if table else "")
        + f" (SQLSTATE {sqlstate})"
        + "; blocking pid unavailable after lock refusal",
        sqlstate=sqlstate,
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
