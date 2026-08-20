"""Bounded HTTP client shared by real controller-backed acceptance slices."""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAXIMUM_RESPONSE_BYTES = 2 * 1024 * 1024


class SliceError(RuntimeError):
    """A bounded, secret-free acceptance failure."""


def validate_base_url(value: str, label: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SliceError(f"{label} URL is invalid")
    if parsed.scheme == "http":
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as error:
            raise SliceError(
                f"{label} plain HTTP requires an explicit loopback address"
            ) from error
        if not address.is_loopback:
            raise SliceError(
                f"{label} plain HTTP requires an explicit loopback address"
            )
    return value.rstrip("/")


class Client:
    def __init__(self, base: str, token: str, *, timeout: float) -> None:
        self.base = base
        self._token = token
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        payload: object | bytes | None = None,
        *,
        content_type: str = "application/json",
        allowed: tuple[int, ...] = (200, 201, 202),
        query: dict[str, str | int] | None = None,
    ) -> tuple[int, object | None]:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise SliceError("request path is invalid")
        data: bytes | None
        if payload is None:
            data = None
        elif isinstance(payload, bytes):
            data = payload
        else:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        query_string = urllib.parse.urlencode(query or {})
        request_path = path + (f"?{query_string}" if query_string else "")
        request = urllib.request.Request(
            self.base + request_path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": content_type,
                "Accept": "application/json",
            },
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
            status = response.status
            body = response.read(MAXIMUM_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            body = error.read(MAXIMUM_RESPONSE_BYTES + 1)
        except (OSError, urllib.error.URLError) as error:
            raise SliceError(f"request failed: {method} {path}") from error
        if status not in allowed:
            raise SliceError(f"request failed: {method} {path} returned HTTP {status}")
        if len(body) > MAXIMUM_RESPONSE_BYTES:
            raise SliceError(f"request failed: {method} {path} response is too large")
        if not body:
            return status, None
        try:
            return status, json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SliceError(
                f"request failed: {method} {path} response is invalid"
            ) from error


def require_object(value: object | None, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SliceError(f"{label} response is invalid")
    return value
