"""Bounded accessors for JSON that arrived as ``object``.

Persisted and decoded JSON reaches the Controller typed as ``object``. The
canonical Pydantic model is the authority for any document that has one; these
helpers exist for the bounded, schema-light reads around it, so a read either
returns the shape the caller asked for or is handled explicitly instead of
being forced through a cast.

Every accessor accepts ``object`` and returns ``None`` on a shape mismatch.
Callers decide whether that is a default, a skip, or an error, which keeps the
decision visible at the call site.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = ["BoundedJSONError", "integer", "mapping", "require_mapping", "sequence", "text"]


class BoundedJSONError(ValueError):
    """A bounded JSON read did not have the declared shape."""


def mapping(value: object) -> Mapping[str, object] | None:
    """Return *value* as a JSON object, or ``None`` when it is not one."""

    return value if isinstance(value, Mapping) else None


def sequence(value: object) -> Sequence[object] | None:
    """Return *value* as a JSON array, or ``None`` when it is not one.

    Strings and bytes are sequences in Python but are never JSON arrays, so
    they are rejected rather than silently iterated character by character.
    """

    if isinstance(value, (str, bytes, bytearray)):
        return None
    return value if isinstance(value, Sequence) else None


def integer(value: object, *, default: int | None = None) -> int | None:
    """Return *value* as an integer, or *default* when it is not one.

    ``bool`` is an ``int`` subclass but is never a JSON integer count, so it is
    rejected instead of becoming 0 or 1.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def text(value: object, *, default: str | None = None) -> str | None:
    """Return *value* as a string, or *default* when it is not one."""

    return value if isinstance(value, str) else default


def require_mapping(value: object, detail: str) -> Mapping[str, object]:
    """Return *value* as a JSON object or raise :class:`BoundedJSONError`."""

    result = mapping(value)
    if result is None:
        raise BoundedJSONError(detail)
    return result
