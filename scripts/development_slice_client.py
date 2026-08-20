"""Bounded HTTP client shared by real controller-backed acceptance slices."""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

MAXIMUM_RESPONSE_BYTES = 2 * 1024 * 1024
Transport = Callable[
    [str, str, bytes | None, dict[str, str], float], tuple[int, bytes]
]


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
    def __init__(
        self,
        base: str,
        token: str | None,
        *,
        timeout: float,
        headers: Mapping[str, str] | None = None,
        transport: Transport | None = None,
    ) -> None:
        fixed_headers = {} if headers is None else dict(headers)
        if (
            timeout <= 0
            or (token is None) == (not fixed_headers)
            or (token is not None and (not token or "\0" in token or "\r" in token or "\n" in token))
            or any(
                not isinstance(name, str)
                or not isinstance(value, str)
                or not name
                or name.lower() in {"accept", "content-length", "content-type", "host"}
                or any(character in name for character in "\0\r\n:")
                or any(character in value for character in "\0\r\n")
                for name, value in fixed_headers.items()
            )
        ):
            raise SliceError("client authentication is invalid")
        self.base = base
        self._headers = (
            {"Authorization": f"Bearer {token}"} if token is not None else fixed_headers
        )
        self.timeout = timeout
        self._transport = transport

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
        request_headers = {
            "Accept": "application/json",
            "Content-Type": content_type,
            **self._headers,
        }
        if self._transport is not None:
            try:
                status, body = self._transport(
                    method, request_path, data, request_headers, self.timeout
                )
            except OSError as error:
                raise SliceError(f"request failed: {method} {path}") from error
            if (
                not isinstance(status, int)
                or isinstance(status, bool)
                or not isinstance(body, bytes)
            ):
                raise SliceError(f"request failed: {method} {path}")
        else:
            request = urllib.request.Request(
                self.base + request_path,
                data=data,
                method=method,
                headers=request_headers,
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
