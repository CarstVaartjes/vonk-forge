"""Bounded in-request attempts: the one pause schedule request-led retries share.

A request that meets a transient refusal (a lock another writer holds for a few
milliseconds, a release channel that is being published) retries the whole
operation a bounded number of times before it reports the refusal to its
requester.  The caller owns the loop, so each attempt starts from a clean
transaction and the request's own identity makes a repeat idempotent::

    refused: SomeUnknown | None = None
    for _attempt in bounded_attempts():
        try:
            return self._once(...)
        except SomeUnknown as error:
            refused = error
    assert refused is not None
    raise refused

The handler never raises: the last refusal is re-raised only after the attempts
are spent.  This is the in-request counterpart of the lifecycle core's backoff
(``lifecycle.core``), which owns retries that outlive a request.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator, Sequence

#: Pause before the second and third attempt (seconds), jittered by 0.5x to 1.5x.
REQUEST_PAUSES: tuple[float, ...] = (0.05, 0.15)


def bounded_attempts(
    pauses: Sequence[float] = REQUEST_PAUSES,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[int]:
    """Yield ``len(pauses) + 1`` attempt numbers, pausing before each repeat."""

    for attempt in range(len(pauses) + 1):
        if attempt:
            sleep(pauses[attempt - 1] * (0.5 + random.random()))
        yield attempt
