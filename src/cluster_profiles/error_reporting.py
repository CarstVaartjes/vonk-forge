"""Bounded, source preserving context for errors at the control boundary.

This module contains no wire model.  HTTP payloads continue to be validated by
the generated OpenAPI models; this is the local diagnostic representation used
by the client and CLI.
"""

from __future__ import annotations

import http.client
import re
import socket
import ssl
import urllib.error
import urllib.parse
from dataclasses import dataclass
from typing import Literal

ErrorSource = Literal["remote_rejection", "transport", "local_io", "protocol", "unknown"]
TransportKind = Literal["dns", "connect", "tls", "timeout", "body", "protocol"]
ErrorDecision = Literal["retry", "defer", "exit"]

_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,95}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def safe_endpoint(value: object) -> str | None:
    """Return only a URL path, dropping credentials and query components."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    path = parsed.path if parsed.scheme or parsed.netloc else value.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/") or "\x00" in path:
        return None
    return path[:512]


def safe_code(value: object, fallback: str = "controller.request_failed") -> str:
    """Keep only the canonical code grammar used by Controller problems."""

    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    return fallback


def safe_request_id(value: object) -> str | None:
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        return None
    return value


def safe_path(value: object) -> str | None:
    """Bound an operational path without reading its contents."""

    text = str(value) if value is not None else ""
    if not text or "\x00" in text or any(character in text for character in "\r\n"):
        return None
    return text[:512]


def classify_transport_error(error: BaseException) -> TransportKind | None:
    """Classify only exception types that prove a transport failure source."""

    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(error, ssl.SSLError):
        return "tls"
    if isinstance(error, socket.gaierror):
        return "dns"
    if isinstance(error, (ConnectionError, ConnectionRefusedError, ConnectionResetError)):
        return "connect"
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, BaseException):
            return classify_transport_error(reason)
        return None
    if isinstance(error, http.client.HTTPException):
        return "body"
    return None


def decision_for(*, retryable: bool, retry_after_seconds: int | None = None, source: ErrorSource = "unknown") -> ErrorDecision:
    if retryable:
        return "defer" if retry_after_seconds is not None else "retry"
    if source == "remote_rejection" and retry_after_seconds is not None:
        return "defer"
    return "exit"


@dataclass(frozen=True, slots=True)
class ErrorContext:
    """Safe context carried by a surfaced error."""

    operation: str
    endpoint: str | None
    code: str
    source: ErrorSource
    decision: ErrorDecision
    retryable: bool = False
    http_status: int | None = None
    request_id: str | None = None
    transport: TransportKind | None = None
    errno: int | None = None
    path: str | None = None

    def __post_init__(self) -> None:
        if not self.operation or len(self.operation) > 160:
            raise ValueError("error operation is invalid")
        if self.endpoint is not None and safe_endpoint(self.endpoint) != self.endpoint:
            raise ValueError("error endpoint must be a path")
        if safe_code(self.code) != self.code:
            raise ValueError("error code is invalid")
        if self.request_id is not None and safe_request_id(self.request_id) != self.request_id:
            raise ValueError("error request ID is invalid")
        if self.http_status is not None and not 100 <= self.http_status <= 599:
            raise ValueError("error HTTP status is invalid")
        if self.errno is not None and not isinstance(self.errno, int):
            raise ValueError("error errno is invalid")
        if self.path is not None and safe_path(self.path) != self.path:
            raise ValueError("error path is invalid")

    def as_dict(self) -> dict[str, object]:
        """Return an omission-preserving CLI/log representation."""

        values: dict[str, object] = {
            "operation": self.operation,
            "endpoint": self.endpoint,
            "http_status": self.http_status,
            "code": self.code,
            "request_id": self.request_id,
            "source": self.source,
            "transport": self.transport,
            "retryable": self.retryable,
            "decision": self.decision,
            "errno": self.errno,
            "path": self.path,
        }
        return {key: value for key, value in values.items() if value is not None}

    def render(self, detail: str) -> str:
        bounded = detail.replace("\x00", "").strip()[:256] or "request failed"
        status = f" HTTP {self.http_status}" if self.http_status is not None else ""
        request = f" request_id={self.request_id}" if self.request_id else ""
        return f"{self.operation} {self.code}{status}: {bounded}{request} [{self.decision}]"


def transport_context(*, operation: str, endpoint: object, error: BaseException) -> ErrorContext:
    kind = classify_transport_error(error)
    return ErrorContext(
        operation=operation,
        endpoint=safe_endpoint(endpoint),
        code=f"controller.transport_{kind or 'unknown'}",
        source="transport",
        transport=kind,
        retryable=kind in {"dns", "connect", "timeout"},
        decision=decision_for(
            retryable=kind in {"dns", "connect", "timeout"}, source="transport"
        ),
        errno=getattr(error, "errno", None) if isinstance(getattr(error, "errno", None), int) else None,
    )


def local_io_context(*, operation: str, path: object, error: BaseException) -> ErrorContext:
    """Build a context that names only the validated operational path."""

    errno = getattr(error, "errno", None)
    return ErrorContext(
        operation=operation,
        endpoint=None,
        code="local.io_failed",
        source="local_io",
        decision="exit",
        errno=errno if isinstance(errno, int) else None,
        path=safe_path(path),
    )


def protocol_context(*, operation: str, endpoint: object = None) -> ErrorContext:
    return ErrorContext(
        operation=operation,
        endpoint=safe_endpoint(endpoint),
        code="controller.protocol_invalid",
        source="protocol",
        decision="exit",
    )


__all__ = [
    "ErrorContext",
    "ErrorDecision",
    "ErrorSource",
    "TransportKind",
    "classify_transport_error",
    "decision_for",
    "local_io_context",
    "protocol_context",
    "safe_code",
    "safe_endpoint",
    "safe_path",
    "safe_request_id",
    "transport_context",
]
