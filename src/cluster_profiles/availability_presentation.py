"""Neutral presentation views for durable Model/Recipe availability operations.

The Controller client boundary owns generated request/response types.  This
module only turns a decoded JSON document into bounded CLI display values and
never chooses a different Model, Recipe revision, or source.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_AUTHORIZATION = re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+")
_SENSITIVE = re.compile(r"(?i)\b(token|api[_-]?key|secret|password)\s*[:=]\s*[^\s,;]+")
_SIGNED_URL = re.compile(r"https?://[^\s?]+\?[^\s]+")


def _text(value: object, *, maximum: int, fallback: str = "") -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    return value.replace("\x00", "").strip()[:maximum]


def _number(value: object, *, integer: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    if integer and not isinstance(value, int):
        return None
    return value


def _log(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    safe = _AUTHORIZATION.sub(r"\1<redacted>", value)
    safe = _SENSITIVE.sub(lambda match: f"{match.group(1)}=<redacted>", safe)
    safe = _SIGNED_URL.sub("<signed-url-redacted>", safe)
    return safe.replace("\x00", "").strip()[:1024] or None


@dataclass(frozen=True, slots=True)
class AvailabilityFailureView:
    code: str
    detail: str
    recovery_actions: tuple[str, ...]
    retryable: bool
    retry_time: str | None
    retry_after_seconds: int | None
    log_excerpt: str | None
    required_bytes: int | None
    free_bytes: int | None
    shortfall_bytes: int | None


@dataclass(frozen=True, slots=True)
class AvailabilityProgressView:
    phase: str
    completed_bytes: int
    total_bytes_known: bool
    total_bytes: int | None
    bytes_per_second: float | None
    eta_seconds: float | None
    step: str | None
    log_excerpt: str | None


@dataclass(frozen=True, slots=True)
class AvailabilityMemberView:
    key: str
    state: str
    progress: AvailabilityProgressView
    failure: AvailabilityFailureView | None


@dataclass(frozen=True, slots=True)
class AvailabilityOperationView:
    id: str
    request_id: str
    recipe_revision_id: str
    state: str
    attempt: int
    progress: AvailabilityProgressView
    members: tuple[AvailabilityMemberView, ...]
    failure: AvailabilityFailureView | None
    result: Mapping[str, Any] | None
    actions: tuple[str, ...]


def parse_availability_failure(value: object) -> AvailabilityFailureView | None:
    if not isinstance(value, Mapping):
        return None
    recovery = value.get("recovery_actions")
    actions = tuple(_text(item, maximum=64) for item in recovery if isinstance(item, str)) if isinstance(recovery, Sequence) and not isinstance(recovery, (str, bytes)) else ()
    raw = value
    def bytes_field(name: str) -> int | None:
        number = _number(raw.get(name), integer=True)
        return number if isinstance(number, int) else None
    retry_after = _number(value.get("retry_after_seconds"), integer=True)
    return AvailabilityFailureView(
        code=_text(value.get("code"), maximum=128, fallback="availability.operation_failed"),
        detail=_text(value.get("detail"), maximum=512, fallback="Availability operation failed."),
        recovery_actions=actions[:8],
        retryable=value.get("retryable") is True,
        retry_time=_text(value.get("retry_time"), maximum=64) or None,
        retry_after_seconds=retry_after if isinstance(retry_after, int) else None,
        log_excerpt=_log(value.get("log_excerpt")),
        required_bytes=bytes_field("required_bytes"),
        free_bytes=bytes_field("free_bytes"),
        shortfall_bytes=bytes_field("shortfall_bytes"),
    )


def parse_availability_progress(value: object) -> AvailabilityProgressView:
    raw = value if isinstance(value, Mapping) else {}
    total = _number(raw.get("total_bytes"), integer=True)
    total_known = raw.get("total_bytes_known") is True or total is not None
    completed = _number(raw.get("completed_bytes"), integer=True)
    rate = _number(raw.get("bytes_per_second"))
    eta = _number(raw.get("eta_seconds"))
    return AvailabilityProgressView(
        phase=_text(raw.get("phase"), maximum=64, fallback="preparing"),
        completed_bytes=completed if isinstance(completed, int) else 0,
        total_bytes_known=total_known,
        total_bytes=total if isinstance(total, int) else None,
        bytes_per_second=float(rate) if isinstance(rate, (int, float)) else None,
        eta_seconds=float(eta) if isinstance(eta, (int, float)) else None,
        step=_text(raw.get("step"), maximum=256) or None,
        log_excerpt=_log(raw.get("log_excerpt")),
    )


def parse_availability_operation(value: object) -> AvailabilityOperationView | None:
    if not isinstance(value, Mapping):
        return None
    raw_children = value.get("children")
    children: list[AvailabilityMemberView] = []
    if isinstance(raw_children, Sequence) and not isinstance(raw_children, (str, bytes)):
        for child in raw_children:
            if not isinstance(child, Mapping):
                continue
            children.append(AvailabilityMemberView(
                key=_text(child.get("kind"), maximum=64, fallback="availability-member"),
                state=_text(child.get("state"), maximum=32, fallback="queued"),
                progress=parse_availability_progress(child.get("progress")),
                failure=parse_availability_failure(child.get("failure")),
            ))
    result = value.get("result")
    return AvailabilityOperationView(
        id=_text(value.get("id"), maximum=128),
        request_id=_text(value.get("request_id"), maximum=128),
        recipe_revision_id=_text(value.get("recipe_revision_id"), maximum=128),
        state=_text(value.get("state"), maximum=32, fallback="queued"),
        attempt=int(_number(value.get("attempt"), integer=True) or 0),
        progress=parse_availability_progress(value.get("progress")),
        members=tuple(children),
        failure=parse_availability_failure(value.get("failure")),
        result=dict(result) if isinstance(result, Mapping) else None,
        actions=tuple(_text(item, maximum=64) for item in value.get("actions", ()) if isinstance(item, str)) if isinstance(value.get("actions"), Sequence) and not isinstance(value.get("actions"), (str, bytes)) else (),
    )


def select_availability_operation(values: Sequence[AvailabilityOperationView], recipe_revision_id: str) -> AvailabilityOperationView | None:
    matching = [value for value in values if value.recipe_revision_id == recipe_revision_id]
    if not matching:
        return None
    terminal = {"succeeded", "failed", "cancelled"}
    matching.sort(key=lambda value: (value.state in terminal, -value.attempt))
    return matching[0]


__all__ = [
    "AvailabilityFailureView",
    "AvailabilityMemberView",
    "AvailabilityOperationView",
    "AvailabilityProgressView",
    "parse_availability_failure",
    "parse_availability_operation",
    "parse_availability_progress",
    "select_availability_operation",
]
