from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from typing import Self

import httpx
import pytest

from cluster_profiles.control_client import (
    ControlClient,
    ControlClientError,
    ControlMalformedResponse,
    ControlUnauthorized,
    _RecordingTransport,
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


def _artifact_job_response() -> dict[str, object]:
    return {
        "id": "12345678-1234-4123-8123-123456789abc",
        "run_id": "12345678-1234-4123-8123-123456789abc",
        "interface": "image-job",
        "state": "draft",
        "contract_sha256": "a" * 64,
        "compiled_contract": {},
        "input_manifest_sha256": "b" * 64,
        "input_total_bytes": 0,
        "input_declarations": [],
        "input_files": [],
        "output_limits": {
            "allowed_media_types": ["image/png"],
            "max_file_bytes": 1,
            "max_files": 1,
            "max_total_bytes": 1,
        },
        "output_files": [],
        "timeout_seconds": 1,
        "created_at": "2026-09-07T00:00:00Z",
        "updated_at": "2026-09-07T00:00:00Z",
    }


def test_raw_request_encodes_bounded_query_parameters(tmp_path: Path) -> None:
    observed: list[object] = []

    def opener(request, *, timeout: float):
        observed.extend((request, timeout))
        return _Response(200, {"jobs": [], "total": 0, "next_cursor": None})

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )

    result = client.request(
        "GET",
        "/api/v1/jobs",
        query={"cursor": "next page", "status": "waiting-for-operator"},
    )

    assert result == {"jobs": [], "total": 0, "next_cursor": None}
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


@pytest.mark.parametrize(
    "problem",
    [{"detail": 7}, {"detail": "bad", "unexpected": True}],
)
def test_raw_request_rejects_malformed_typed_errors(
    tmp_path: Path, problem: dict[str, object]
) -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    body = io.BytesIO(json.dumps(problem).encode())

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

    with pytest.raises(ControlMalformedResponse, match="OpenAPI schema"):
        client.request("GET", "/api/v1/jobs")


def test_raw_request_rejects_error_fields_outside_openapi_contract(tmp_path: Path) -> None:
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

    with pytest.raises(ControlMalformedResponse, match="OpenAPI schema"):
        client.request(
            "POST",
            "/api/v1/model-cache/download",
            {
                "request_key": "11111111-1111-4111-8111-111111111111",
                "plan_digest": "a" * 64,
            },
        )

def test_request_validates_canonical_route_models_and_preserves_204(
    tmp_path: Path,
) -> None:
    capabilities = {
        "schema_version": 1,
        "storage": {
            "in_flight_uploads": 0,
            "max_stored_bytes": 1024,
            "remaining_bytes": 1024,
            "reserved_bytes": 0,
            "used_bytes": 0,
        },
        "transport": {
            "max_input_file_bytes": 512,
            "max_input_files": 32,
            "max_input_total_bytes": 1024,
            "max_output_file_bytes": 1024,
            "max_output_files": 32,
            "max_output_total_bytes": 2048,
            "max_timeout_seconds": 3600,
            "reserved_input_names": ["manifest.json"],
        },
    }
    observed: list[bytes | None] = []

    def opener(request, *, timeout: float):
        observed.append(request.data)
        if request.full_url.endswith("/artifact-jobs/capabilities"):
            return _Response(200, capabilities)
        return _Response(204, None)

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )

    result = client.request(
        "GET",
        "/api/v1/artifact-jobs/capabilities",
    )
    assert result == capabilities

    assert client.request(
        "POST", "/api/v1/agents/nodes/spk_node/revoke"
    ) == {}
    assert observed[-1] is None


def test_request_rejects_undocumented_no_content_status(tmp_path: Path) -> None:
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _Response(204, None),
    )

    with pytest.raises(ControlMalformedResponse, match="undocumented status"):
        client.request("GET", "/api/v1/artifact-jobs/capabilities")


def test_request_rejects_response_outside_canonical_route_model(tmp_path: Path) -> None:
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _Response(
            200,
            {"schema_version": 1, "storage": {}, "transport": {}},
        ),
    )

    with pytest.raises(ControlMalformedResponse, match="OpenAPI schema"):
        client.request(
            "GET",
            "/api/v1/artifact-jobs/capabilities",
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["storage"].update(max_stored_bytes="1024"),
        lambda payload: payload.update(unexpected=True),
    ],
)
def test_request_rejects_scalar_and_unknown_response_fields(
    tmp_path: Path, mutation
) -> None:
    payload = {
        "schema_version": 1,
        "storage": {
            "in_flight_uploads": 0,
            "max_stored_bytes": 1024,
            "remaining_bytes": 1024,
            "reserved_bytes": 0,
            "used_bytes": 0,
        },
        "transport": {
            "max_input_file_bytes": 512,
            "max_input_files": 32,
            "max_input_total_bytes": 1024,
            "max_output_file_bytes": 1024,
            "max_output_files": 32,
            "max_output_total_bytes": 2048,
            "max_timeout_seconds": 3600,
            "reserved_input_names": ["manifest.json"],
        },
    }
    mutation(payload)
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _Response(200, payload),
    )

    with pytest.raises(ControlMalformedResponse, match="OpenAPI schema"):
        client.request(
            "GET",
            "/api/v1/artifact-jobs/capabilities",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"reason": 7},
        {"reason": "operator requested cancellation", "unexpected": True},
    ],
)
def test_request_rejects_scalar_and_unknown_request_fields(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _Response(204, None),
    )

    with pytest.raises(ControlClientError, match="OpenAPI schema"):
        client.request(
            "POST",
            "/api/v1/artifact-jobs/job-1/cancel",
            payload,
        )


def test_request_rejects_undocumented_success_status(tmp_path: Path) -> None:
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _Response(299, {
            "schema_version": 1,
            "storage": {
                "in_flight_uploads": 0,
                "max_stored_bytes": 1024,
                "remaining_bytes": 1024,
                "reserved_bytes": 0,
                "used_bytes": 0,
            },
            "transport": {
                "max_input_file_bytes": 512,
                "max_input_files": 32,
                "max_input_total_bytes": 1024,
                "max_output_file_bytes": 1024,
                "max_output_files": 32,
                "max_output_total_bytes": 2048,
                "max_timeout_seconds": 3600,
                "reserved_input_names": ["manifest.json"],
            },
        }),
    )

    with pytest.raises(ControlMalformedResponse, match="undocumented status"):
        client.request("GET", "/api/v1/artifact-jobs/capabilities")


def test_request_rejects_route_missing_from_bundled_openapi(tmp_path: Path) -> None:
    client = ControlClient("https://forge.example.test", _token(tmp_path))

    with pytest.raises(ControlClientError, match="not in the bundled schema"):
        client.request("GET", "/api/v1/retired-route")


def test_request_rejects_json_body_on_binary_route(tmp_path: Path) -> None:
    client = ControlClient("https://forge.example.test", _token(tmp_path))

    with pytest.raises(ControlClientError, match="does not accept application/json"):
        client.request(
            "PUT",
            "/api/v1/artifact-jobs/job-1/inputs/prompt.txt",
            {"content": "not bytes"},
        )


def test_generated_transport_uses_raw_openapi_contract_before_attrs_parser(
    tmp_path: Path,
) -> None:
    valid = {
        "authority_revision": "a" * 64,
        "evidence_digest": "b" * 64,
        "nodes": [],
    }
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _Response(200, valid),
    )

    result = client.nodes()

    assert result.to_dict() == valid


@pytest.mark.parametrize(
    "payload",
    [
        {"authority_revision": 7, "evidence_digest": "b" * 64, "nodes": []},
        {
            "authority_revision": "a" * 64,
            "evidence_digest": "b" * 64,
            "nodes": [],
            "unexpected": True,
        },
    ],
)
def test_generated_transport_rejects_malformed_raw_response(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _Response(200, payload),
    )

    with pytest.raises(ControlMalformedResponse, match="OpenAPI schema"):
        client.nodes()


def test_generated_transport_rejects_malformed_request_before_network() -> None:
    class _CountingTransport(httpx.BaseTransport):
        calls = 0

        def handle_request(self, _request: httpx.Request) -> httpx.Response:
            self.calls += 1
            raise AssertionError("malformed request reached the network")

    underlying = _CountingTransport()
    transport = _RecordingTransport(underlying)
    request = httpx.Request(
        "POST",
        "https://forge.example.test/api/v1/model-cache/download",
        headers={"Content-Type": "application/json"},
        content=json.dumps(
            {
                "request_key": 7,
                "plan_digest": "a" * 64,
            }
        ).encode(),
    )

    with pytest.raises(ControlClientError, match="OpenAPI schema"):
        transport.handle_request(request)

    assert underlying.calls == 0


def test_generated_transport_rejects_malformed_typed_error(tmp_path: Path) -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    body = io.BytesIO(json.dumps({"detail": 7}).encode())

    def opener(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://forge.example.test/api/v1/nodes/status",
            401,
            "Unauthorized",
            headers,
            body,
        )

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )

    with pytest.raises(ControlMalformedResponse, match="OpenAPI schema"):
        client.nodes()


def test_openapi_validation_is_safe_for_concurrent_requests(
    tmp_path: Path,
) -> None:
    capabilities = {
        "schema_version": 1,
        "storage": {
            "in_flight_uploads": 0,
            "max_stored_bytes": 1024,
            "remaining_bytes": 1024,
            "reserved_bytes": 0,
            "used_bytes": 0,
        },
        "transport": {
            "max_input_file_bytes": 512,
            "max_input_files": 32,
            "max_input_total_bytes": 1024,
            "max_output_file_bytes": 1024,
            "max_output_files": 32,
            "max_output_total_bytes": 2048,
            "max_timeout_seconds": 3600,
            "reserved_input_names": ["manifest.json"],
        },
    }
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _Response(200, capabilities),
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _index: client.request(
                    "GET",
                    "/api/v1/artifact-jobs/capabilities",
                ),
                range(32),
            )
        )

    assert results == [capabilities] * 32


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
        return _Response(200, _artifact_job_response())

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

    assert result["state"] == "draft"
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
