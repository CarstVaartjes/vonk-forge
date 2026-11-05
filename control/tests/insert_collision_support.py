"""Make two real admissions collide on the database's unique key."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import event
from sqlalchemy.engine import Engine


def collide_inserts[T](
    engine: Engine,
    table: str,
    request_field: str,
    key: str,
    calls: tuple[Callable[[], T], Callable[[], T]],
) -> list[T]:
    """Run both calls, holding each at its ``INSERT INTO table`` of ``key``.

    Both calls have passed every absence check before either row is written,
    so the unique key decides the race and the loser takes its conflict path
    every run, not only when the winner happens to commit late. A call that
    ends without reaching its insert releases its partner at once, and its own
    error is raised instead of the partner's broken barrier.
    """

    barrier = threading.Barrier(2, timeout=30)
    arrivals: list[int] = []

    def before_insert(_conn, _cursor, statement, parameters, _context, _many):
        if (
            statement.startswith(f"INSERT INTO {table} ")
            and isinstance(parameters, Mapping)
            and parameters.get(request_field) == key
        ):
            arrivals.append(threading.get_ident())
            barrier.wait()

    def run(call: Callable[[], T]) -> T:
        try:
            return call()
        finally:
            # Only a call that never arrived can strand its partner; one that
            # arrived has passed the barrier together with it.
            if threading.get_ident() not in arrivals:
                barrier.abort()

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
    assert len(arrivals) == 2, f"{len(arrivals)} of 2 calls reached INSERT INTO {table}"
    return [future.result() for future in futures]
