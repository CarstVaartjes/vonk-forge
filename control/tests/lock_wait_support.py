"""Prove by behaviour that work never queues behind a held PostgreSQL lock."""

from __future__ import annotations

from concurrent.futures import Future, wait

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker


def backend_pid(session: Session) -> int:
    """The PostgreSQL backend that owns ``session``'s open transaction."""

    pid = session.scalar(text("SELECT pg_backend_pid()"))
    assert isinstance(pid, int)
    return pid


def lock_waits_behind(
    sessions: sessionmaker[Session], holder_pid: int, work: Future
) -> set[tuple[object, ...]]:
    """The lock requests seen queued behind ``holder_pid`` until ``work`` ends.

    ``pg_locks`` is read live in a fresh transaction per sample, so a session
    the work opens later is still seen. A genuine lock wait lasts until the
    holder ends or ``lock_timeout`` fires, far longer than one sample, so this
    replaces a wall-clock bound that a loaded runner can exceed without waiting.
    """

    waits: set[tuple[object, ...]] = set()
    with sessions() as observer:
        while not waits and not wait([work], timeout=0.005).done:
            waits.update(
                tuple(row)
                for row in observer.execute(
                    text(
                        "SELECT pid, locktype, mode FROM pg_locks "
                        "WHERE NOT granted AND :holder = ANY(pg_blocking_pids(pid))"
                    ),
                    {"holder": holder_pid},
                )
            )
            observer.rollback()
    return waits
