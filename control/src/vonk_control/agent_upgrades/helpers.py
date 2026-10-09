"""Agent upgrades: helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from .constants import _ALREADY_CURRENT


def _summary(skipped: Mapping[str, str]) -> str | None:
    """Explain what a concluded rollout left untouched, never inventing work."""

    if not skipped:
        return None
    if all(reason == _ALREADY_CURRENT for reason in skipped.values()):
        return "selected Sparks already running the requested agent build were left unchanged"
    passed = sorted(
        (node_id, reason)
        for node_id, reason in skipped.items()
        if reason != _ALREADY_CURRENT
    )
    listed = "; ".join(f"Spark {node_id} {reason}" for node_id, reason in passed[:4])
    more = f" (+{len(passed) - 4} more)" if len(passed) > 4 else ""
    return f"skipped: {listed}{more}"[:1024]


def _failure_detail(result: object) -> str | None:
    """The failed attempt's own explanation, for the operator-visible reason."""

    if not isinstance(result, Mapping):
        return None
    parts: list[str] = []
    for key in ("reason", "error_code"):
        value = result.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
            break
    for key in ("helper_error_code", "diagnostic"):
        value = result.get(key)
        if isinstance(value, str) and value and value not in " ".join(parts):
            parts.append(f"{key}={value}")
    exit_code = result.get("helper_exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        parts.append(f"helper_exit_code={exit_code}")
    return "; ".join(parts)[:256] or None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
