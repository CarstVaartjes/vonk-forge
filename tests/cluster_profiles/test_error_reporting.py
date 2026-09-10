from __future__ import annotations

import io
import json
import socket
import ssl
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

from cluster_profiles.control_client import (
    ControlClient,
    ControlForbidden,
    ControlTransportError,
    ControlUnauthorized,
)
from cluster_profiles.error_reporting import (
    ErrorContext,
    classify_transport_error,
    local_io_context,
    safe_endpoint,
)


def _token(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text("private-token")
    path.chmod(0o600)
    return path


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (socket.gaierror(-2, "name or service not known"), "dns"),
        (ConnectionRefusedError(111, "connection refused"), "connect"),
        (ssl.SSLError("certificate verify failed"), "tls"),
        (TimeoutError("timed out"), "timeout"),
    ],
)
def test_transport_classifier_preserves_proven_source(error: BaseException, kind: str) -> None:
    assert classify_transport_error(error) == kind


def test_unknown_transport_is_explicit() -> None:
    assert classify_transport_error(OSError("opaque failure")) is None


def test_local_io_keeps_safe_path_and_errno() -> None:
    context = local_io_context(
        operation="read artifact input",
        path="/var/lib/vonk/inputs/image.png",
        error=OSError(13, "permission denied"),
    )
    assert context.as_dict()["path"] == "/var/lib/vonk/inputs/image.png"
    assert context.as_dict()["errno"] == 13
    assert context.source == "local_io"


def test_endpoint_drops_query_and_userinfo() -> None:
    assert safe_endpoint("https://user:secret@example.test/api/jobs?token=secret") == "/api/jobs"


def test_error_context_omits_unavailable_request_id() -> None:
    context = ErrorContext(
        operation="GET /api/jobs",
        endpoint="/api/jobs",
        code="controller.transport_timeout",
        source="transport",
        transport="timeout",
        decision="retry",
        retryable=True,
    )
    assert "request_id" not in context.as_dict()
    assert context.as_dict()["decision"] == "retry"


@pytest.mark.parametrize("status", [401, 403])
def test_http_errors_keep_status_code_and_request_id(tmp_path: Path, status: int) -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["X-Request-ID"] = "00000000-0000-4000-8000-000000000099"
    code = (
        "controller.authentication_required"
        if status == 401
        else "controller.request_rejected"
    )
    body = io.BytesIO(
        json.dumps(
            {
                "detail": "bad Bearer private-token",
                "context": {
                    "operation": "GET /api/jobs",
                    "endpoint": "/api/jobs",
                    "http_status": status,
                    "code": code,
                    "request_id": "00000000-0000-4000-8000-000000000099",
                    "source": "remote_rejection",
                    "decision": "exit",
                },
            }
        ).encode()
    )

    def opener(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://forge.example.test/api/jobs?secret=private-token",
            status,
            "rejected",
            headers,
            body,
        )

    client = ControlClient("https://forge.example.test", _token(tmp_path), opener=opener)
    expected = ControlUnauthorized if status == 401 else ControlForbidden
    with pytest.raises(expected) as raised:
        client.request("GET", "/api/jobs/00000000-0000-4000-8000-000000000001/logs")
    assert raised.value.context is not None
    assert raised.value.context.http_status == status
    assert raised.value.context.code == code
    assert raised.value.request_id == "00000000-0000-4000-8000-000000000099"
    assert "private-token" not in str(raised.value)


def test_transport_error_does_not_swallow_source(tmp_path: Path) -> None:
    def opener(*_args, **_kwargs):
        raise urllib.error.URLError(socket.gaierror(-2, "no such host"))

    client = ControlClient("https://forge.example.test", _token(tmp_path), opener=opener)
    with pytest.raises(ControlTransportError) as raised:
        client.request("GET", "/api/jobs")
    assert raised.value.context is not None
    assert raised.value.context.transport == "dns"
    assert raised.value.context.decision == "retry"
