from __future__ import annotations

import asyncio
import io
import json
import socket
import ssl
import time
import urllib.error
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path

import pytest
from vonk_control.fleet_projection import FleetSnapshot
from vonk_control.observation_transfer import (
    OBSERVATION_MEDIA_TYPE,
    observation_response,
)

from cluster_profiles import cli
from cluster_profiles.control_client import ControlClient, ControlTransportError
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


def test_local_io_keeps_safe_path_and_errno() -> None:
    context = local_io_context(
        operation="read artifact input",
        path="/var/lib/vonk/inputs/image.png",
        error=OSError(13, "permission denied"),
    )
    assert context.as_dict()["path"] == "/var/lib/vonk/inputs/image.png"
    assert context.as_dict()["errno"] == 13


def test_endpoint_drops_query_and_userinfo() -> None:
    assert (
        safe_endpoint("https://user:secret@example.test/api/fleet?token=secret")
        == "/api/fleet"
    )


def test_error_context_omits_unavailable_request_id() -> None:
    context = ErrorContext(
        operation="GET /api/fleet",
        endpoint="/api/fleet",
        code="controller.transport_timeout",
        source="transport",
        transport="timeout",
        decision="retry",
        retryable=True,
    )
    assert "request_id" not in context.as_dict()


class _FleetReply(io.BytesIO):
    status = 200

    def __init__(self):
        snapshot = FleetSnapshot(
            authority_revision="a" * 64,
            event_cursor=0,
            generated_at=datetime(2026, 10, 9, tzinfo=UTC),
            nodes=[],
        )
        response = observation_response(snapshot, resource="fleet")

        async def collect() -> bytes:
            parts: list[bytes] = []
            async for part in response.body_iterator:
                parts.append(part.encode() if isinstance(part, str) else bytes(part))
            return b"".join(parts)

        super().__init__(asyncio.run(collect()))
        self.headers = Message()
        self.headers["Content-Type"] = OBSERVATION_MEDIA_TYPE

    def __exit__(self, *args: object) -> None:
        self.close()


@pytest.mark.parametrize("status", [401, 403])
def test_http_denial_preserves_correlation_without_effect_then_fresh_read_works(
    tmp_path: Path, status: int, capsys
) -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["X-Request-ID"] = "00000000-0000-4000-8000-000000000099"
    denied = True
    calls = []

    def opener(*_args, **_kwargs):
        calls.append(None)
        if not denied:
            return _FleetReply()
        raise urllib.error.HTTPError(
            "https://forge.example.test/api/fleet?secret=private-token",
            status,
            "rejected",
            headers,
            io.BytesIO(json.dumps({"detail": "bad Bearer private-token"}).encode()),
        )

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    assert cli.main(("fleet", "--json"), control_client=client) == 2
    output = capsys.readouterr().out
    document = json.loads(output)
    assert document["http_status"] == status
    assert document["request_id"] == "00000000-0000-4000-8000-000000000099"
    assert "private-token" not in output
    assert len(calls) == 1
    denied = False
    assert cli.main(("fleet", "--json"), control_client=client) == 0
    assert json.loads(capsys.readouterr().out)["nodes"] == []
    assert len(calls) == 2


@pytest.mark.parametrize(
    "fault",
    [
        socket.gaierror(-2, "no such host"),
        ConnectionRefusedError(111, "connection refused"),
        ssl.SSLError("certificate verify failed"),
        TimeoutError("timed out"),
        OSError("opaque failure"),
    ],
)
def test_transport_loss_is_reobserved_and_fresh_read_is_admitted(
    tmp_path: Path, monkeypatch, capsys, fault
) -> None:
    calls = []
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )

    def opener(*_args, **_kwargs):
        calls.append(None)
        if len(calls) == 1:
            raise urllib.error.URLError(fault)
        return _FleetReply()

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    assert cli.main(("fleet", "--json"), control_client=client) == 0
    assert json.loads(capsys.readouterr().out)["nodes"] == []
    assert len(calls) == 2
    assert clock[0] <= 30
    assert cli.main(("fleet", "--json"), control_client=client) == 0
    assert len(calls) == 3


def test_transport_error_preserves_source_at_deadline_then_fresh_read_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the real bounded retry loop without spending its deadline asleep.
    now = [100.0]
    delays: list[float] = []
    attempts = 0
    unavailable = True
    fault = socket.gaierror(-2, "no such host")

    def sleep(seconds: float) -> None:
        delays.append(seconds)
        now[0] += seconds

    # Retry and streamed observation share the same monotonic deadline.
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", sleep)

    def opener(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if unavailable:
            raise urllib.error.URLError(fault)
        return _FleetReply()

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    with pytest.raises(ControlTransportError) as raised:
        client.request("GET", "/api/fleet")
    assert raised.value.context is not None
    assert raised.value.context.transport == classify_transport_error(fault)
    assert raised.value.context.retryable is True
    assert attempts > 1
    assert now[0] == pytest.approx(100.0 + client.request_timeout_seconds)
    assert len(delays) > 1
    assert all(delay > 0 for delay in delays)
    ended_attempts = attempts
    unavailable = False
    assert client.request("GET", "/api/fleet")["nodes"] == []
    assert attempts == ended_attempts + 1
