"""Strict JSON and bounded fixture scalar validation."""

from __future__ import annotations

import json
from collections.abc import Mapping

from .contracts import FixtureError


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FixtureError(f"{label} must be an object")
    return value


def _strict_json_loads(content: bytes | str) -> object:
    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number: {value}")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        content,
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise FixtureError(f"{label} is invalid")
    return value


def _integer_value(
    mapping: Mapping[str, object],
    key: str,
    label: str,
    *,
    default: int | None = None,
) -> int:
    """Return a required (or defaulted) integer contract field or fail loudly."""

    value = mapping.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise FixtureError(f"{label} {key} is invalid")
    return value


def _number_value(
    mapping: Mapping[str, object],
    key: str,
    label: str,
    *,
    default: float | None = None,
) -> float:
    """Return a required (or defaulted) JSON number contract field as a float."""

    value = mapping.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise FixtureError(f"{label} {key} is invalid")
    return float(value)


def _string_list_value(
    mapping: Mapping[str, object],
    key: str,
    label: str,
    *,
    default: list[str] | None = None,
) -> list[str]:
    """Return an array-of-strings contract field or fail loudly."""

    value = mapping.get(key, default)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise FixtureError(f"{label} {key} is invalid")
    return value


def _numeric_token(value: object, label: str) -> float:
    """Parse an ffprobe numeric token, which is normally a JSON string.

    A JSON boolean or a structured value is a malformed measurement, so it
    fails loudly instead of becoming ``0`` or ``1``.
    """

    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise FixtureError(f"{label} is unavailable")
    try:
        return float(value)
    except ValueError as error:
        raise FixtureError(f"{label} is unavailable") from error


def _positive_number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise FixtureError(f"{label} is invalid")
    return float(value)


def _number_range(assertion: Mapping[str, object], key: str, label: str) -> None:
    minimum = assertion.get(f"minimum_{label}")
    maximum = assertion.get(f"maximum_{label}")
    if minimum is None and maximum is None:
        return
    low = _positive_number(minimum, f"minimum {label}")
    high = _positive_number(maximum, f"maximum {label}")
    if low > high:
        raise FixtureError(f"recipe fixture {key} {label} range is invalid")
