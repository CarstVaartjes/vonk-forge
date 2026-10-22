"""Deduplicated storage reads within a profile response's two-second budget.

Only observation entry points install this scope. Admission and workers keep
reading the storage owner directly. Timed-out probes hold bounded I/O slots,
not request workers; another read joins the same probe until it finishes.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from functools import wraps
from threading import Lock
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session, SessionTransaction
from vonk_agent_protocol import WaitReason

from .categorized_errors import BookkeepingUnknown

PROFILE_READ_STORAGE_SECONDS = 2.0
_WORKERS = 8
_POOL = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="profile-storage")
_LOCK = Lock()
# Only live probes live here, so hung storage cannot create an unbounded queue.
_IN_FLIGHT: dict[tuple[Any, ...], Future[Any]] = {}
# Retain one completed result per I/O slot for five seconds. A read can collect
# a probe that completed after its predecessor timed out, without extending the
# freshness window or retaining an unbounded result cache.
_RECENT: OrderedDict[tuple[Any, ...], tuple[Future[Any], float]] = OrderedDict()
_FRESH_SECONDS = 5.0


def _finished(key: tuple[Any, ...], future: Future[Any]) -> None:
    with _LOCK:
        if _IN_FLIGHT.get(key) is not future:
            return
        _IN_FLIGHT.pop(key)
        if not future.cancelled() and future.exception() is None:
            _RECENT[key] = (future, time.monotonic() + _FRESH_SECONDS)
            while len(_RECENT) > _WORKERS:
                _RECENT.popitem(last=False)


_WORKER: ContextVar[bool] = ContextVar("profile_storage_worker", default=False)


@dataclass
class _Read:
    deadline: float
    identity: object = field(default_factory=object)
    results: dict[tuple[Any, ...], Future[Any]] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)


_READ: ContextVar[_Read | None] = ContextVar("profile_storage_read", default=None)


@event.listens_for(Session, "after_transaction_create")
def _statement_only_observation(
    session: Session, transaction: SessionTransaction
) -> None:
    """A profile read owns no PostgreSQL transaction between SQL statements.

    Preview is revalidated at acceptance; it never held a repeatable-read
    snapshot. Use statement transactions, including nested planner sessions,
    so source parsing, storage probes and their bounded waits cannot idle an
    outer SQL transaction. This context is copied into storage workers too.
    """
    if _READ.get() is not None and transaction.parent is None:
        session.connection(execution_options={"isolation_level": "AUTOCOMMIT"})


@event.listens_for(Session, "do_orm_execute")
def _observation_select_only(execution: ORMExecuteState) -> None:
    if _READ.get() is not None:
        if not execution.is_select:
            raise RuntimeError("Profile observation attempted a database write")
        # A server-side cursor requires a transaction across Python iteration.
        # Buffer this statement before returning to the projection instead.
        execution.update_execution_options(yield_per=None, stream_results=False)


@event.listens_for(Session, "before_flush")
def _observation_no_flush(
    session: Session, _context: object, _instances: object
) -> None:
    if _READ.get() is not None:
        raise RuntimeError("Profile observation attempted a database flush")


def _pending() -> BookkeepingUnknown:
    return BookkeepingUnknown(
        "Cache observation is pending after the profile storage budget; retry this read. "
        "No missing or ready asset is inferred from the pending check.",
        reason=WaitReason.OBSERVATION_UNAVAILABLE,
    )


def profile_read[**P, T](function: Callable[P, T]) -> Callable[P, T]:
    """All nested projections share one deadline and one object observation."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        if _READ.get() is not None:
            return function(*args, **kwargs)
        token = _READ.set(_Read(time.monotonic() + PROFILE_READ_STORAGE_SECONDS))
        try:
            return function(*args, **kwargs)
        finally:
            _READ.reset(token)

    return wrapped


def observe_storage[**P, T](
    function: Callable[P, T], *, share_between_reads: bool = True
) -> Callable[P, T]:
    """Observe a hashable immutable identity once; never share SQL sessions."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        read = _READ.get()
        if read is None:
            return function(*args, **kwargs)

        def run() -> T:
            token = _WORKER.set(True)
            try:
                return function(*args, **kwargs)
            finally:
                _WORKER.reset(token)

        key = (function, *args, *sorted(kwargs.items()))
        if not share_between_reads:
            # SQL selection is mutable: another review must not join even an
            # unfinished selection of this revision's previous inputs.
            key = (*key, read.identity)
        inline = False
        with read.lock, _LOCK:
            future = read.results.get(key)
            if future is None:
                future = _IN_FLIGHT.get(key)
                recent = _RECENT.get(key) if share_between_reads else None
                if (
                    future is None
                    and recent is not None
                    and recent[1] > time.monotonic()
                ):
                    future = recent[0]
                if future is None:
                    if time.monotonic() >= read.deadline:
                        raise _pending()
                    if _WORKER.get():
                        # A nested storage read executes in its parent's slot;
                        # parents never wait for children queued in their pool.
                        future = Future()
                        inline = True
                    elif len(_IN_FLIGHT) >= _WORKERS:
                        raise _pending()
                    else:
                        context = copy_context()
                        future = _POOL.submit(context.run, run)
                    _IN_FLIGHT[key] = future
                read.results[key] = future
        if inline:
            try:
                future.set_result(function(*args, **kwargs))
            except Exception as error:  # noqa: BLE001 -- preserve the original typed cause
                future.set_exception(error)
            finally:
                _finished(key, future)
        else:

            def finished(_future: Future[T]) -> None:
                _finished(key, _future)

            future.add_done_callback(finished)
        try:
            return future.result(timeout=max(0.0, read.deadline - time.monotonic()))
        except TimeoutError:
            raise _pending() from None

    return wrapped


def observe_selection[**P, T](function: Callable[P, T]) -> Callable[P, T]:
    """Bound a mutable selection, deduplicating only within the current read."""
    return observe_storage(function, share_between_reads=False)
