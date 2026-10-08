"""Terminal presentation for common responses."""

from __future__ import annotations

import shutil
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime


def terminal_text(value: str) -> str:
    """Make untrusted terminal controls visible instead of executing them."""
    return "".join(
        (f"\\x{ord(char):02x}" if ord(char) <= 255 else f"\\u{ord(char):04x}")
        if unicodedata.category(char) in {"Cc", "Cf"}
        else char
        for char in value
    )


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} is not a valid object")
    return value


def _optional(value: object, field: str) -> Mapping[str, object]:
    return {} if value is None else _object(value, field)


def _records(document: Mapping[str, object], field: str) -> list[Mapping[str, object]]:
    value = document.get(field)
    if not isinstance(value, list) or any(
        not isinstance(row, Mapping) for row in value
    ):
        raise ValueError(f"{field} is not a valid list of records")
    return value


def _text(value: object) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (Mapping, list, tuple)):
        raise TypeError("structured data requires its own presentation")
    encoding = sys.stdout.encoding or "utf-8"
    return (
        terminal_text(str(value)).encode(encoding, "backslashreplace").decode(encoding)
    )


def _warn(message: str) -> None:
    encoding = sys.stderr.encoding or "utf-8"
    print(
        message.encode(encoding, "backslashreplace").decode(encoding), file=sys.stderr
    )


def _projection_issues(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(issue, str) for issue in value
    ):
        raise TypeError("expected a list of projection issues")
    return [str(issue) for issue in value]


def _words(value: object) -> str:
    if value is None:
        return "unavailable"
    if not isinstance(value, (list, tuple)):
        raise TypeError("expected a list of values")
    return ", ".join(_text(item) for item in value) if value else "none"


def _field(label: str, value: object) -> None:
    print(f"{label}: {_text(value)}")


def _bytes(value: object) -> str:
    if value is None:
        return "unavailable"
    if type(value) is not int or value < 0:
        raise ValueError("byte count is invalid")
    for unit, divisor in (
        ("TiB", 1 << 40),
        ("GiB", 1 << 30),
        ("MiB", 1 << 20),
        ("KiB", 1 << 10),
    ):
        if value >= divisor:
            return f"{value / divisor:.1f} {unit} ({value} bytes)"
    return f"{value} B"


def _headroom(value: object) -> str:
    if type(value) is int and value < 0:
        return f"short by {_bytes(-value)}"
    return _bytes(value)


def _time(value: object) -> str:
    if value is None:
        return "unavailable"
    if not isinstance(value, str):
        raise TypeError("timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        # Some operation owners supply opaque historical timestamp text.
        return _text(value)
    if parsed.tzinfo is None:
        return _text(value) + " (timezone unavailable)"
    return parsed.isoformat(sep=" ")


def _freshness(observed_at: object, projected_at: object) -> str:
    if not isinstance(observed_at, str) or not isinstance(projected_at, str):
        return "unavailable"
    try:
        observed = datetime.fromisoformat(observed_at)
        projected = datetime.fromisoformat(projected_at)
    except ValueError:
        return "unavailable"
    if observed.tzinfo is None or projected.tzinfo is None:
        return "timezone unavailable"
    age_seconds = (projected - observed).total_seconds()
    if age_seconds < 0:
        return "route observation is newer than the Controller projection"
    age = int(age_seconds)
    unit, count = (
        ("hour", age // 3600)
        if age >= 3600
        else ("minute", age // 60)
        if age >= 60
        else ("second", age)
    )
    suffix = "" if count == 1 else "s"
    return f"observed {count} {unit}{suffix} before this Controller read"


def _width(value: str) -> int:
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in value
    )


def _table(labels: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    """Use a table when all values fit; otherwise preserve stacked values."""
    rendered = [[_text(value) for value in row] for row in rows]
    if not rendered:
        return
    widths = [
        max(_width(label), *(_width(row[i]) for row in rendered))
        for i, label in enumerate(labels)
    ]
    # A terminal too narrow for the table gets stacked records; a pipe gets
    # the table, one line per row, whatever its width.
    columns = shutil.get_terminal_size((80, 24)).columns
    if sys.stdout.isatty() and (
        columns < 80 or sum(widths) + 2 * (len(labels) - 1) > columns
    ):
        for row in rendered:
            for label, value in zip(labels, row, strict=True):
                _field(label, value)
            print()
        return
    for row in (list(labels), *rendered):
        print(
            "  ".join(
                value + " " * (widths[i] - _width(value)) for i, value in enumerate(row)
            ).rstrip()
        )


def _actions(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise TypeError("next actions are not a list")
    for item in value:
        # Recipe availability uses typed {key} actions; other owners use strings.
        _field("Next", item.get("key") if isinstance(item, Mapping) else item)


def _recommendation(item: Mapping[str, object]) -> str:
    """The Controller's own next step for a warning, when it names one."""
    recommendation = item.get("recommendation")
    return f" {_text(recommendation)}" if recommendation is not None else ""


def _reasons(value: object, *, subject: object = None) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise TypeError("reasons are not a list")
    for item in value:
        if _is_update_notice(item):
            # Informational: the recipe's own sentence says what to do.
            _warn(f"Note: {_text(item.get('detail'))}")
            continue
        if isinstance(item, Mapping):
            message = f"{_text(item.get('code'))}: {_text(item.get('detail'))}"
            message += _recommendation(item)
        else:
            message = _text(item)
        prefix = f"{_text(subject)}: " if subject is not None else ""
        _warn(f"Attention: {prefix}{message}")


def _failure(value: object) -> None:
    if value is None:
        return
    failure = _object(value, "failure")
    _warn(f"Blocker: {_text(failure.get('code'))}: {_text(failure.get('detail'))}")
    for key, label in (
        ("required_bytes", "Required"),
        ("free_bytes", "Available"),
        ("shortfall_bytes", "Shortfall"),
    ):
        if failure.get(key) is not None:
            _warn(f"{label}: {_bytes(failure[key])}")
    if failure.get("retry_time") is not None:
        _warn(f"Next attempt: {_time(failure['retry_time'])}")
    elif failure.get("retry_after_seconds") is not None:
        _warn(f"Retry after: {_text(failure['retry_after_seconds'])} seconds")
    _actions(failure.get("recovery_actions"))


def _waiting(payload: Mapping[str, object]) -> None:
    """What a queued or blocked operation waits for, and when it is checked again."""
    blockers = payload.get("blockers")
    if isinstance(blockers, list):
        for item in blockers:
            if not isinstance(item, Mapping):
                continue
            nodes = item.get("node_ids")
            where = f" [{_words(nodes)}]" if isinstance(nodes, list) and nodes else ""
            _field(
                "Waiting for",
                f"{_text(item.get('code'))}: {_text(item.get('detail'))}{where}",
            )
    if payload.get("next_attempt_at") is not None:
        _field("Next attempt", _time(payload["next_attempt_at"]))


def _is_update_notice(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("code") == _UPDATE_CODE


_UPDATE_CODE = "recipe.update_available"
