"""Safe, stable failure summaries for profile observations."""

from __future__ import annotations

import re

from .logging import redact_text


def _error_summary(error: BaseException) -> str:
    """A short, safe description of an error: its type and the start of its text."""

    code = getattr(error, "code", None)
    name = code if isinstance(code, str) and code else type(error).__name__
    text = redact_text(" ".join(str(error).split()))[:160]
    return f"{name}: {text}" if text else name


_VARIABLE_TEXT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|\b[0-9a-f]{12,}\b|\d+"
)


def _normalized_failure_text(value: object) -> str:
    """A failure reason with its changing parts (ids, counts, times) removed."""

    return " ".join(_VARIABLE_TEXT.sub("#", str(value or "")).split())[:240]
