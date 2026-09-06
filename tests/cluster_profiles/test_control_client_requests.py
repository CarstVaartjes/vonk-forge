from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Self

import pytest

from cluster_profiles.control_client import (
    ControlClient,
    ControlForbidden,
    ControlMalformedResponse,
    ControlUnauthorized,
)


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


class _StreamResponse:
    def __init__(
        self, body: bytes, *, media_type: str, sha256: str, size: int | None = None
    ) -> None:
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = media_type
        self.headers["Content-Length"] = str(len(body) if size is None else size)
        self.headers["X-Content-SHA256"] = sha256
        self._body = io.BytesIO(body)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, maximum: int) -> bytes:
        return self._body.read(maximum)


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


def test_raw_request_preserves_shared_availability_error_metadata(tmp_path: Path) -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    body = io.BytesIO(json.dumps({
        "code": "model_cache.auth_required",
        "detail": "Hugging Face access is required",
        "recovery_actions": ["open_model_access", "check_access_and_resume"],
        "required_bytes": 200,
        "free_bytes": 100,
        "shortfall_bytes": 100,
        "retry_time": "2026-09-06T13:05:00Z",
        "preserved": "12 MiB of verified bytes",
    }).encode())

    def opener(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://forge.example.test/api/v1/model-cache/download",
            403,
            "Forbidden",
            headers,
            body,
        )

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )

    with pytest.raises(ControlForbidden) as raised:
        client.request("POST", "/api/v1/model-cache/download", {})

    assert raised.value.code == "model_cache.auth_required"
    assert raised.value.recovery == ("open_model_access", "check_access_and_resume")
    assert raised.value.retry_time == "2026-09-06T13:05:00Z"
    assert raised.value.preserved == "12 MiB of verified bytes"
    assert raised.value.required_bytes == 200
    assert raised.value.free_bytes == 100
    assert raised.value.shortfall_bytes == 100


def test_artifact_input_upload_streams_the_reverified_local_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "prompt.txt"
    content = b"bounded prompt\n"
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    observed: list[object] = []

    def opener(request, *, timeout: float):
        observed.extend(
            (
                request.full_url,
                request.get_header("Content-type"),
                request.get_header("X-content-sha256"),
                request.data.read(),
                timeout,
            )
        )
        return _Response(200, {"state": "draft"})

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )

    result = client.upload_file(
        "/api/v1/artifact-jobs/job-1/inputs/prompt.txt",
        source,
        media_type="text/plain",
        expected_sha256=digest,
        expected_size=len(content),
    )

    assert result == {"state": "draft"}
    assert observed == [
        "https://forge.example.test/api/v1/artifact-jobs/job-1/inputs/prompt.txt",
        "text/plain",
        digest,
        content,
        3_600,
    ]


def test_artifact_output_download_is_verified_and_atomically_published(
    tmp_path: Path,
) -> None:
    content = b"verified output"
    digest = hashlib.sha256(content).hexdigest()
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _StreamResponse(
            content, media_type="image/png", sha256=digest
        ),
    )
    destination = tmp_path / "result.png"

    result = client.download_file(
        f"/api/v1/artifact-jobs/job-1/results/{digest}",
        destination,
        media_type="image/png",
        expected_sha256=digest,
        expected_size=len(content),
        overwrite=False,
    )

    assert destination.read_bytes() == content
    assert result == {
        "destination": str(destination),
        "media_type": "image/png",
        "size_bytes": len(content),
        "sha256": digest,
    }
    assert list(tmp_path.glob(".result.png.*.download")) == []


def test_artifact_output_download_fails_closed_without_partial_file(
    tmp_path: Path,
) -> None:
    content = b"corrupted output"
    expected = hashlib.sha256(b"expected output").hexdigest()
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _StreamResponse(
            content, media_type="image/png", sha256=expected
        ),
    )
    destination = tmp_path / "result.png"

    with pytest.raises(ControlMalformedResponse, match="does not match"):
        client.download_file(
            f"/api/v1/artifact-jobs/job-1/results/{expected}",
            destination,
            media_type="image/png",
            expected_sha256=expected,
            expected_size=len(content),
            overwrite=False,
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".result.png.*.download")) == []
