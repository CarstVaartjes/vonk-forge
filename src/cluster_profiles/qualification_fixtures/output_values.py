"""Output JSON, fraction, range, and required-key checks."""

from __future__ import annotations

import json
from collections.abc import Mapping

from .contracts import FixtureError
from .values import (
    _number_value,
    _numeric_token,
    _object,
    _strict_json_loads,
    _string_list_value,
)


def _parse_fraction(value: object, label: str) -> float:
    if not isinstance(value, str) or "/" not in value:
        raise FixtureError(f"{label} is invalid")
    numerator, denominator = value.split("/", 1)
    try:
        result = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError) as error:
        raise FixtureError(f"{label} is invalid") from error
    if result <= 0:
        raise FixtureError(f"{label} is invalid")
    return result


def _assert_number_range(
    metadata: Mapping[str, object],
    assertion: Mapping[str, object],
    field: str,
    label: str,
    *,
    metadata_field: str | None = None,
) -> None:
    if f"minimum_{field}" not in assertion:
        return
    try:
        raw_value = metadata[metadata_field or field]
    except KeyError as error:
        raise FixtureError(f"artifact {label} {field} is unavailable") from error
    value = _numeric_token(raw_value, f"artifact {label} {field}")
    minimum = _number_value(assertion, f"minimum_{field}", "assertion")
    maximum = _number_value(assertion, f"maximum_{field}", "assertion")
    if not minimum <= value <= maximum:
        raise FixtureError(f"artifact {label} {field} assertion failed")


def _parse_json_object(content: bytes, label: str) -> Mapping[str, object]:
    try:
        value = _strict_json_loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise FixtureError(f"{label} is invalid") from error
    return _object(value, label)


def _assert_required_keys(
    document: Mapping[str, object], assertion: Mapping[str, object]
) -> None:
    required = _string_list_value(
        assertion, "required_keys", "JSON assertion", default=[]
    )
    if any(key not in document for key in required):
        raise FixtureError("artifact JSON required-key assertion failed")
    equals = assertion.get("equals")
    if isinstance(equals, Mapping) and any(
        document.get(key) != value for key, value in equals.items()
    ):
        raise FixtureError("artifact JSON semantic assertion failed")
