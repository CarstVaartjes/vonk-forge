"""Independent CLI selection reads run at once and keep their own outcomes."""

from __future__ import annotations

import time

import pytest

from cluster_profiles.controller_cli.selection import _read_concurrently

_DELAY = 0.3


def _slow(value: object):
    def read() -> object:
        time.sleep(_DELAY)
        return value

    return read


def test_independent_reads_finish_in_about_one_delay() -> None:
    started = time.monotonic()
    outcomes = _read_concurrently({name: _slow(name) for name in ("a", "b", "c")})
    elapsed = time.monotonic() - started

    assert [outcomes.get(name) for name in ("a", "b", "c")] == ["a", "b", "c"]
    assert elapsed < _DELAY * 2


def test_one_failing_read_does_not_hide_the_others() -> None:
    def broken() -> object:
        time.sleep(_DELAY)
        raise ValueError("fleet is unavailable")

    outcomes = _read_concurrently({"a": _slow("a"), "b": broken, "c": _slow("c")})

    assert outcomes.get("a") == "a"
    assert outcomes.get("c") == "c"
    with pytest.raises(ValueError, match="fleet is unavailable"):
        outcomes.get("b")


def test_a_single_read_keeps_its_error() -> None:
    def broken() -> object:
        raise OSError("down")

    with pytest.raises(OSError):
        _read_concurrently({"only": broken}).get("only")
