"""Consumer boundary witness: a rejected call must never return an adopted value.

Effect preservation and fresh admission belong in the scenario using this helper.
Assertions raised by the scenario itself must still fail the test.
"""

from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def not_adopted() -> Iterator[list[Exception]]:
    failures: list[Exception] = []
    try:
        yield failures
    except (AssertionError, NameError, AttributeError, ImportError):
        raise
    except Exception as failure:  # noqa: BLE001 -- outcome witness deliberately ignores failure taxonomy
        failures.append(failure)
        return
    raise AssertionError("invalid or incomplete input was adopted")
