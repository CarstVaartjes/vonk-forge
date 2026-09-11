"""Boundary behaviour of the accessors that read decoded JSON.

These helpers decide whether a persisted or decoded JSON value is the shape a
caller asked for. Getting one of these cases wrong does not raise: it silently
turns a string into a character list, ``true`` into ``1``, or a missing count
into a default, which is exactly the class of fault the accessors exist to
prevent. Each case below fails on a specific wrong implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import pytest
from vonk_control.bounded_json import (
    BoundedJSONError,
    integer,
    mapping,
    require_integer,
    require_mapping,
    require_sequence,
    sequence,
    text,
)


class _MappingSubclass(Mapping[str, object]):
    """A real Mapping that is not a dict."""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class _DuckMapping:
    """Mapping-*like*, but not a ``collections.abc.Mapping``.

    ``isinstance(x, Mapping)`` does not accept a class that merely implements
    ``__getitem__``/``__iter__``/``__len__``, so this is deliberately not a
    mapping as far as the accessors are concerned.
    """

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def test_a_decoded_string_is_not_a_json_array() -> None:
    """A string is a Sequence, so iterating one yields characters.

    This is the reason ``sequence`` has an explicit str/bytes guard: a
    persisted ``"abc"`` where an array belongs must not become ``["a","b","c"]``.
    """

    for not_an_array in ("abc", "", b"abc", bytearray(b"abc")):
        assert sequence(not_an_array) is None


def test_a_json_array_decoded_as_a_tuple_is_still_an_array() -> None:
    """A driver may hand back a tuple for a JSON array.

    Treating that as malformed would reject valid persisted state, so ``tuple``
    has to pass.
    """

    assert sequence([1, 2]) == [1, 2]
    assert sequence((1, 2)) == (1, 2)


def test_non_arrays_are_rejected() -> None:
    for not_an_array in ({}, {"a": 1}, 1, 1.5, None, True):
        assert sequence(not_an_array) is None


def test_a_json_object_is_read_through_the_mapping_abc() -> None:
    """Any Mapping counts, not only ``dict`` — but only a real Mapping.

    The accessor trusts ``collections.abc.Mapping`` rather than probing for
    mapping-like methods, so a lazy wrapper that does not derive from it is
    rejected. That is a deliberate choice worth pinning: if a future decoder
    starts returning such a wrapper, these reads fail loudly instead of
    silently returning ``None`` for every field.
    """

    assert mapping({"a": 1}) == {"a": 1}
    assert mapping(MappingProxyType({"a": 1})) == {"a": 1}
    assert mapping(_MappingSubclass({"a": 1})) is not None
    assert mapping(_DuckMapping({"a": 1})) is None
    for not_an_object in ([], "a", b"a", 1, None, True):
        assert mapping(not_an_object) is None


def test_boolean_is_never_an_integer_count() -> None:
    """``bool`` subclasses ``int``, so ``True`` would otherwise count as 1."""

    assert integer(True) is None
    assert integer(False) is None
    assert integer(True, default=7) == 7


def test_zero_and_negative_counts_are_kept() -> None:
    """Zero is a real count, not a missing value."""

    assert integer(0) == 0
    assert integer(0, default=7) == 0
    assert integer(-3) == -3


def test_non_integers_fall_back_to_the_declared_default() -> None:
    for not_an_integer in (1.0, "1", b"1", None, [], {}, True):
        assert integer(not_an_integer, default=5) == 5
        assert integer(not_an_integer) is None


def test_text_keeps_an_empty_string() -> None:
    """An empty string is a value; returning the default would hide it."""

    assert text("") == ""
    assert text("v", default="d") == "v"
    for not_text in (0, 1, b"v", None, [], True):
        assert text(not_text, default="d") == "d"
        assert text(not_text) is None


def test_require_variants_raise_the_caller_detail() -> None:
    """A required read must fail loudly, naming the document it came from."""

    assert require_mapping({"a": 1}, "job document") == {"a": 1}
    assert require_sequence([1], "target list") == [1]
    assert require_integer(3, "node count") == 3

    with pytest.raises(BoundedJSONError, match="job document"):
        require_mapping([], "job document")
    with pytest.raises(BoundedJSONError, match="target list"):
        require_sequence("abc", "target list")
    with pytest.raises(BoundedJSONError, match="node count"):
        require_integer(True, "node count")


def test_a_required_integer_does_not_accept_a_default() -> None:
    """The fail-loud form must not have a fallback to fall back to."""

    with pytest.raises(BoundedJSONError):
        require_integer(0.0, "count")
    assert require_integer(0, "count") == 0


def test_the_error_stays_a_value_error() -> None:
    """Callers already catch ValueError at their JSON boundary.

    Widening this away from ValueError would turn a handled contract failure
    into an unhandled one, so the base class is part of the contract.
    """

    assert issubclass(BoundedJSONError, ValueError)
