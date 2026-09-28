"""Central structured redaction for Controller logs."""

from __future__ import annotations

import json
import logging as stdlib_logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

#: The API boundary binds each request's correlation id here, so a handler that
#: only sees an exception can still name the request in its log line.
current_request_id: ContextVar[str | None] = ContextVar(
    "vonk_control_request_id", default=None
)

_SENSITIVE_KEY = re.compile(
    r"(?i)(authorization|api.?key|password|secret|token|private.?key|credential)"
)
_AUTHORIZATION = re.compile(r"(?i)authorization\s*:\s*(?:bearer|basic)\s+[^\s,;]+")
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+")
_ASSIGNMENT = re.compile(r"(?i)\b(password|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+")
_PRIVATE_BLOCK = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL
)
_HTTP_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_MAX_FIELD = 4096


class _CurrentStderrHandler(stdlib_logging.StreamHandler):
    """Follow the process stderr stream so test and supervisor capture stays live."""

    def emit(self, record: stdlib_logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def configure_controller_logging() -> None:
    """Emit Controller structured INFO events without enabling dependency noise."""

    for name in (
        "vonk_control",
        "vonk-control-worker",
        "vonk-control-run-switch",
    ):
        logger = stdlib_logging.getLogger(name)
        logger.setLevel(stdlib_logging.INFO)
        logger.propagate = False
        handler = next(
            (
                current
                for current in logger.handlers
                if getattr(current, "_vonk_controller_handler", False)
            ),
            None,
        )
        if handler is None:
            handler = _CurrentStderrHandler()
            handler.setFormatter(stdlib_logging.Formatter("%(message)s"))
            handler._vonk_controller_handler = True  # type: ignore[attr-defined]
            logger.addHandler(handler)
        handler.setLevel(stdlib_logging.INFO)


def _redact_url(match: re.Match[str]) -> str:
    # Download redirects often carry credentials under provider-specific query
    # names. Keep the source useful for diagnosis without guessing those names.
    value = match.group(0)
    url = value.rstrip(".,;)]}")
    suffix = value[len(url) :]
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<redacted-url>" + suffix
    return (
        urlunsplit(
            (
                parts.scheme,
                parts.netloc.rsplit("@", 1)[-1],
                parts.path,
                "<redacted>" if parts.query else "",
                "<redacted>" if parts.fragment else "",
            )
        )
        + suffix
    )


def redact_text(value: object) -> str:
    text = str(value).replace("\x00", "")
    text = _PRIVATE_BLOCK.sub("<redacted>", text)
    text = _HTTP_URL.sub(_redact_url, text)
    text = _AUTHORIZATION.sub("Authorization: <redacted>", text)
    text = _BEARER.sub("<redacted>", text)
    text = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    if len(text) > _MAX_FIELD:
        text = text[: _MAX_FIELD - len("<truncated>")] + "<truncated>"
    return text


def _safe(value: object, key: str = "") -> object:
    if _SENSITIVE_KEY.search(key):
        return "<redacted>"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            str(child_key): _safe(child, str(child_key))
            for child_key, child in list(value.items())[:64]
        }
    if isinstance(value, (list, tuple)):
        return [_safe(child) for child in value[:64]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value)


def log_event(
    logger: stdlib_logging.Logger, event: str, *, service: str, **fields: object
) -> None:
    if re.fullmatch(r"[a-z][a-z0-9_.-]{1,127}", event) is None:
        raise ValueError("structured log event name is invalid")
    if re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", service) is None:
        raise ValueError("structured log service name is invalid")
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": stdlib_logging.getLevelName(logger.getEffectiveLevel()).lower(),
        "service": service,
        "event": event,
        **{key: _safe(value, key) for key, value in fields.items()},
    }
    logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))
