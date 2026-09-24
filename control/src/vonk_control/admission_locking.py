"""Canonical, nonblocking SQL locks for resource admission transactions."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from .settings import database_wait_budgets

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


class AdmissionLockBusy(RuntimeError):
    """An admission lock could not be acquired without waiting."""

    code = "admission.capacity_busy"


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
    session: Session, keys: Collection[AdmissionLockKey]
) -> None:
    """Try every declared advisory key in canonical order, without waiting."""

    if not keys:
        return
    if session.get_bind().dialect.name != "postgresql":
        return
    _set_local_admission_timeout(session)
    statement = text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))")
    for key in sorted(set(keys)):
        try:
            acquired = session.scalar(statement, {"key": key.database_key})
        except OperationalError as error:
            _raise_if_busy(error)
            raise
        if acquired is not True:
            raise AdmissionLockBusy(f"admission lock {key.namespace} is busy")


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
            statement = statement.with_for_update(of=request.model, nowait=True)
        statement = statement.execution_options(populate_existing=True)
        try:
            locked[request.name] = tuple(session.scalars(statement))
        except OperationalError as error:
            _raise_if_busy(error)
            raise
    return locked


def _set_local_admission_timeout(session: Session) -> None:
    """Apply the centrally configured bound to implicit index/FK waits."""

    timeout = database_wait_budgets().admission_lock_timeout_ms
    session.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": f"{timeout}ms"},
    )


def _raise_if_busy(error: OperationalError) -> None:
    if is_admission_contention(error):
        raise AdmissionLockBusy("admission lock is busy") from error
