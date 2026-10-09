"""HTTPS response reading, token validation, and transport adapters."""

from __future__ import annotations

import http.client
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import replace
from email.message import Message
from pathlib import Path

import httpx2

from ..control_limits import MAX_CONTROL_DOCUMENT_BYTES
from ..error_reporting import (
    local_io_context,
    protocol_context,
    safe_endpoint,
    safe_request_id,
    transport_context,
)
from .errors import (
    _MAX_TOKEN,
    _STATUS_ERRORS,
    ControlClientError,
    ControlMalformedResponse,
    ControlResponseTooLarge,
    ControlTransportError,
    _OpenedResponse,
    _retry_after_seconds,
)
from .schema import (
    _operation,
    _schema_unavailable,
    _validate_generated_request,
    _validate_generated_response,
)


def _read_token_file(token_file: Path) -> str:
    flags = os.O_RDONLY
    # Reject FIFOs/devices after fstat without waiting for another process to
    # open a pipe. Nonblocking mode has no effect on a regular credential file.
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ControlClientError("control token file cannot be opened safely")
    flags |= no_follow

    descriptor = -1
    try:
        descriptor = os.open(token_file, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ControlClientError("control token must be a regular non-symlink file")
        getuid = getattr(os, "getuid", None)
        if getuid is not None and metadata.st_uid != getuid():
            raise ControlClientError("control token file owner is invalid")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ControlClientError("control token file permissions are too broad")

        content = bytearray()
        while len(content) <= _MAX_TOKEN:
            chunk = os.read(descriptor, _MAX_TOKEN + 1 - len(content))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > _MAX_TOKEN:
            raise ControlClientError("control token file is invalid")
        try:
            token = bytes(content).decode().strip()
        except UnicodeDecodeError:
            raise ControlClientError("control token file is invalid") from None
    except ControlClientError:
        raise
    except OSError as error:
        context = local_io_context(
            operation="read control token", path=token_file, error=error
        )
        raise ControlClientError(
            context.render("control token must be a regular non-symlink file"),
            context=context,
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if (
        not token
        or len(token) > _MAX_TOKEN
        or any(character.isspace() for character in token)
    ):
        raise ControlClientError("control token file is invalid")
    return token


def _read_control_response(
    opener: Callable[..., _OpenedResponse],
    request: urllib.request.Request,
    timeout: float,
) -> tuple[int, bytes, Message]:
    """Read a bounded document, preserving headers even when its body is lost."""

    endpoint = safe_endpoint(request.full_url)
    operation = f"{request.get_method()} {endpoint or '/'}"[:160]
    received_status: int | None = None
    received_request_id: str | None = None
    received_retry_after: int | None = None
    try:
        try:
            response = opener(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = (
                response.code
                if isinstance(response, urllib.error.HTTPError)
                else response.status
            )
            response_headers = response.headers
            received_status = status
            received_request_id = safe_request_id(response_headers.get("x-request-id"))
            received_retry_after = _retry_after_seconds(
                response_headers.get("retry-after")
            )
            if status in (401, 403):
                # Authentication is decided by the owner, even if the denial's
                # body is unreadable. Never retry it or wait on its body.
                raise _STATUS_ERRORS[status](
                    status,
                    "control API authorization denied",
                    received_retry_after,
                    code=response_headers.get("x-vonk-error-code"),
                    operation=operation,
                    endpoint=endpoint,
                    request_id=received_request_id,
                )
            content = response.read(MAX_CONTROL_DOCUMENT_BYTES + 1)
    except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
        context = replace(
            transport_context(operation=operation, endpoint=endpoint, error=error),
            http_status=received_status,
            request_id=received_request_id,
        )
        raise ControlTransportError(
            context.render("control API request failed"),
            context=context,
            retry_after_seconds=received_retry_after,
        ) from None
    return status, content, response_headers


def _observation_payload(path: str, method: str) -> str | None:
    operation = _operation(path, method)
    payload = operation.get("x-vonk-observation-payload")
    if method != "GET" or not isinstance(payload, dict):
        return None
    reference = payload.get("$ref")
    if reference not in (
        "#/components/schemas/FleetSnapshot",
        "#/components/schemas/PlatformObservation",
    ):
        _schema_unavailable("observation payload contract is unreadable")
    return str(reference).rsplit("/", 1)[1]


class _OpenerTransport(httpx2.BaseTransport):
    def __init__(
        self,
        opener: Callable[..., _OpenedResponse],
        timeout: float,
        *,
        deadline: float | None = None,
    ) -> None:
        self._opener = opener
        self._timeout = timeout
        self._deadline = deadline

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        outgoing = urllib.request.Request(
            str(request.url),
            data=request.content or None,
            headers=dict(request.headers),
            method=request.method,
        )
        remaining = self._timeout
        if self._deadline is not None:
            remaining = min(remaining, self._deadline - time.monotonic())
            if remaining <= 0:
                raise ControlTransportError("control observation deadline elapsed")
        status, content, headers = _read_control_response(
            self._opener, outgoing, remaining
        )
        if len(content) > MAX_CONTROL_DOCUMENT_BYTES:
            raise ControlResponseTooLarge(
                "control API response exceeds safety limit",
                context=replace(
                    protocol_context(
                        operation=f"{request.method} {safe_endpoint(str(request.url))}",
                        endpoint=str(request.url),
                    ),
                    http_status=status,
                    request_id=safe_request_id(headers.get("x-request-id")),
                ),
                retry_after_seconds=_retry_after_seconds(headers.get("retry-after")),
            )
        return httpx2.Response(
            status,
            content=content,
            headers=httpx2.Headers(headers.items()),
            request=request,
        )


class _RecordingTransport(httpx2.BaseTransport):
    def __init__(self, transport: httpx2.BaseTransport) -> None:
        self._transport = transport
        self.request: httpx2.Request | None = None
        self.response: httpx2.Response | None = None
        self.request_validated = False

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        self.request = request
        _validate_generated_request(request)
        self.request_validated = True
        response = self._transport.handle_request(request)
        self.response = response
        if len(response.content) > MAX_CONTROL_DOCUMENT_BYTES:
            raise ControlResponseTooLarge("control API response exceeds safety limit")
        try:
            _validate_generated_response(request, response)
        except ControlClientError as error:
            raise ControlMalformedResponse(
                str(error),
                context=replace(
                    protocol_context(
                        operation=f"{request.method} {safe_endpoint(str(request.url))}",
                        endpoint=str(request.url),
                    ),
                    http_status=response.status_code,
                    request_id=safe_request_id(response.headers.get("x-request-id")),
                ),
                retry_after_seconds=_retry_after_seconds(
                    response.headers.get("retry-after")
                ),
            ) from None
        return response
