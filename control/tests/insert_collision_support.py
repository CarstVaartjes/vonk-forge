"""Hold concurrent test threads together, and make two real admissions collide.

``Rendezvous`` is the only thread barrier control tests use (the added-line
guard ``barriers`` flags a raw ``threading.Barrier``). A raw barrier placed in a
patched product method waits again when the product calls that method a second
time on the same thread (a replay after a unique-key conflict, a replan); that
thread then waits alone until the timeout breaks the barrier, and the test fails
at random with ``BrokenBarrierError`` instead of naming the re-entry.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import event
from sqlalchemy.engine import Engine


class Rendezvous:
    """A barrier each thread passes once; a second wait on a thread fails at once."""

    def __init__(self, parties: int, *, timeout: float | None = 30) -> None:
        self._barrier = threading.Barrier(parties, timeout=timeout)
        self._lock = threading.Lock()
        self._arrived: set[int] = set()

    def wait(self) -> None:
        thread = threading.get_ident()
        with self._lock:
            if thread in self._arrived:
                raise AssertionError(
                    "this thread already passed the rendezvous: the call it "
                    "holds ran a second time on the same thread"
                )
            self._arrived.add(thread)
        self._barrier.wait()

    @property
    def arrivals(self) -> int:
        with self._lock:
            return len(self._arrived)

    def arrived(self) -> bool:
        """Whether the calling thread has reached the rendezvous."""
        with self._lock:
            return threading.get_ident() in self._arrived

    def abort(self) -> None:
        """Release every waiting thread with ``BrokenBarrierError``."""
        self._barrier.abort()


def collide_inserts[T](
    engine: Engine,
    table: str,
    request_field: str,
    key: str,
    calls: tuple[Callable[[], T], Callable[[], T]],
    *,
    timeout: float | None = 30,
) -> list[T]:
    """Run both calls, holding each at its ``INSERT INTO table`` of ``key``.

    Both calls have passed every absence check before either row is written,
    so the unique key decides the race and the loser takes its conflict path
    every run, not only when the winner happens to commit late. A call that
    ends without reaching its insert releases its partner at once, and its own
    error is raised instead of the partner's broken barrier.
    """

    rendezvous = Rendezvous(2, timeout=timeout)

    def before_insert(_conn, _cursor, statement, parameters, _context, _many):
        if (
            statement.startswith(f"INSERT INTO {table} ")
            and isinstance(parameters, Mapping)
            and parameters.get(request_field) == key
        ):
            rendezvous.wait()

    def run(call: Callable[[], T]) -> T:
        try:
            return call()
        finally:
            # Only a call that never arrived can strand its partner; one that
            # arrived has passed the rendezvous together with it.
            if not rendezvous.arrived():
                rendezvous.abort()

    event.listen(engine, "before_cursor_execute", before_insert)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, call) for call in calls]
    finally:
        event.remove(engine, "before_cursor_execute", before_insert)
    for future in futures:
        error = future.exception()
        if error is not None and not isinstance(error, threading.BrokenBarrierError):
            raise error
    assert rendezvous.arrivals == 2, (
        f"{rendezvous.arrivals} of 2 calls reached INSERT INTO {table}"
    )
    return [future.result() for future in futures]
