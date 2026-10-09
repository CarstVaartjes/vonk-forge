"""Observe a recoverable call without making its exception taxonomy a contract."""

from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def observe_unknown() -> Iterator[None]:
    """The witness is retained bytes, released ownership and fresh admission.

    A call may return a miss or end with an observation error. Tests using this
    scope assert those effects outside it, rather than prescribing an exception.
    """
    try:
        yield
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001 - behavior witnesses below own the outcome
        return
