from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Self

import pytest

from cluster_profiles.control_client import ControlClient, ControlUnauthorized


class _Response:
    def __init__(self, status: int, payload: object | None) -> None:
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self._body = b"" if payload is None else json.dumps(payload).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, maximum: int) -> bytes:
        return self._body[:maximum]


def _token(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text("private-token")
    path.chmod(0o600)
    return path


def test_raw_request_encodes_bounded_query_parameters(tmp_path: Path) -> None:
    observed: list[object] = []

    def opener(request, *, timeout: float):
        observed.extend((request, timeout))
        return _Response(200, {"jobs": []})

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )

    result = client.request(
        "GET",
        "/api/v1/jobs",
        query={"cursor": "next page", "status": "waiting-for-operator"},
    )

    assert result == {"jobs": []}
    assert observed[0].full_url == (
        "https://forge.example.test/api/v1/jobs?cursor=next+page&status=waiting-for-operator"
    )
    assert observed[0].get_header("Authorization") == "Bearer private-token"


def test_raw_request_accepts_no_content_mutation_response(tmp_path: Path) -> None:
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _Response(204, None),
    )

    assert client.request("POST", "/api/v1/agents/nodes/spk_node/revoke") == {}


def test_raw_request_preserves_typed_bounded_api_errors(tmp_path: Path) -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["Retry-After"] = "7"
    body = io.BytesIO(json.dumps({"detail": "bad token private-token"}).encode())

    def opener(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://forge.example.test/api/v1/jobs",
            401,
            "Unauthorized",
            headers,
            body,
        )

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )

    with pytest.raises(ControlUnauthorized) as raised:
        client.request("GET", "/api/v1/jobs")

    assert raised.value.detail == "bad token <redacted>"
    assert raised.value.retry_after_seconds == 7
