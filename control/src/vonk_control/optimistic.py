"""One optimistic transaction boundary for versioned rows.

Models opt in with SQLAlchemy's ``version_id_col``. Every ORM write then emits
``UPDATE ... SET ..., version = :next WHERE id = :id AND version = :read``;
zero matched rows raise ``StaleDataError``. This also fences writers outside
this helper: an unversioned ORM overwrite cannot silently bypass the fence.

The callback reads, decides and writes inside one short transaction. It must
contain only transactional database effects, never HTTP, files or dispatch.
A conflict rolls back *all* effects, then calls the decision once in a new
session (fresh rows, not the old plan). A second conflict returns STALE to the
normal reconcile tick. There is no retry loop, sleep or SELECT FOR UPDATE.
Bulk UPDATE bypasses mapper version checks and must not write versioned tables.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError


@dataclass(frozen=True)
class Applied[T]:
    """The transaction committed; its decision's result is now publishable."""

    value: T


@dataclass(frozen=True)
class Stale:
    """Still contended after one fresh decision; the next tick owns progress."""


def transition[T](
    sessions: sessionmaker[Session], decide: Callable[[Session], T]
) -> Applied[T] | Stale:
    """Commit a decision, or re-plan once and yield on continued contention.

    PostgreSQL UPDATE takes a row lock until commit, not just for 1 ms. The
    callback must do only database work and return promptly; external work uses
    the committed result, after this helper has closed the transaction.

    ``lock_timeout=1ms`` is PostgreSQL's smallest positive millisecond setting:
    approximate NOWAIT for implicit UPDATE locks (zero disables the timeout).
    It bounds each lock acquisition, not execution or transaction duration.
    We prefer yielding a tick to queuing behind a writer; this is not an SLA
    for healthy transaction runtime. The one fresh decision reads again after
    rollback. Continued contention yields to the normal reconcile schedule.
    Only SQLSTATE 55P03 and a lost version fence are stale; permission denial
    (42501), invalid data and other database errors propagate unchanged.
    """

    def attempt() -> Applied[T]:
        with sessions.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(text("SET LOCAL lock_timeout = '1ms'"))
            value = decide(session)
        return Applied(value)

    try:
        return attempt()
    except (StaleDataError, OperationalError) as error:
        _require_conflict(error)
    try:
        return attempt()
    except (StaleDataError, OperationalError) as error:
        _require_conflict(error)
        return Stale()


def _require_conflict(error: StaleDataError | OperationalError) -> None:
    if isinstance(error, StaleDataError):
        return
    if getattr(error.orig, "sqlstate", None) != "55P03":
        raise error
