from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Self, cast

import httpx2
import pytest
from vonk_control.fleet_projection import FleetSnapshot as OwnedFleetSnapshot
from vonk_control.observation_transfer import (
    OBSERVATION_MEDIA_TYPE,
    ObservationTransferChunk,
    ObservationTransferComplete,
    ObservationTransferStart,
    _record,
    observation_response,
)

from cluster_profiles import cli
from cluster_profiles.control_client import (
    ControlClient,
    ControlClientError,
    ControlTimeout,
    _RecordingTransport,
)
from cluster_profiles.generated_control.models.fleet_profile_definition import (
    FleetProfileDefinition,
)
from cluster_profiles.generated_control.models.fleet_profile_effects import (
    FleetProfileEffects,
)
from cluster_profiles.generated_control.models.fleet_profile_plan_summary import (
    FleetProfilePlanSummary,
)
from cluster_profiles.generated_control.models.fleet_profile_preview import (
    FleetProfilePreview,
)
from cluster_profiles.generated_control.models.fleet_profile_scope_preview import (
    FleetProfileScopePreview,
)
from control.tests.observation_transfer_peer import ObservationHTTPPeer


@pytest.fixture(autouse=True)
def observation_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    return now


@contextmanager
def _not_adopted() -> Iterator[list[Exception]]:
    failures: list[Exception] = []
    try:
        yield failures
    except (ControlClientError, OSError) as error:
        failures.append(error)
    assert len(failures) == 1, "an incomplete observation was adopted"


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


def test_request_budget_limits_transport_without_changing_client_default(
    tmp_path: Path,
) -> None:
    timeouts: list[float] = []

    def opener(request, *, timeout):
        timeouts.append(timeout)
        return _Response(200, _artifact_job_response())

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    path = "/api/artifact-jobs/12345678-1234-4123-8123-123456789abc"
    client.request("GET", path, timeout_seconds=0.025)
    client.request("GET", path)
    assert timeouts == pytest.approx([0.025, 15])
    for invalid in (0, -1, float("nan"), float("inf")):
        with _not_adopted():
            client.request("GET", path, timeout_seconds=invalid)
    assert len(timeouts) == 2
    _fresh_request(client)


def _artifact_job_response() -> dict[str, object]:
    return {
        "id": "12345678-1234-4123-8123-123456789abc",
        "run_id": "12345678-1234-4123-8123-123456789abc",
        "interface": "image-job",
        "state": None,
        "preparation": "draft",
        "contract_sha256": "a" * 64,
        "compiled_contract": {
            "schema_version": 1,
            "interface": "image-job",
            "input": {
                "required": False,
                "media_types": [],
                "max_bytes": 0,
                "slots": [],
            },
            "parameters": [],
            "output": {
                "path": "/outputs",
                "max_total_bytes": 1,
                "slots": [
                    {
                        "id": "image",
                        "label": "Image",
                        "description": "Generated image output",
                        "media_types": ["image/png"],
                        "extensions": [".png"],
                        "min_files": 1,
                        "max_files": 1,
                        "max_file_bytes": 1,
                        "max_total_bytes": 1,
                    }
                ],
            },
            "output_limits": {
                "max_files": 1,
                "max_file_bytes": 1,
                "max_total_bytes": 1,
                "allowed_media_types": ["image/png"],
            },
            "max_timeout_seconds": 3600,
        },
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


def _profile_preview_response():
    return FleetProfilePreview(
        allowed=True,
        assignments=[],
        generated_at=datetime(2026, 9, 13, tzinfo=UTC),
        plan_digest="a" * 64,
        effects_digest="a" * 64,
        profile_digest="b" * 64,
        profile_id="12345678-1234-4123-8123-123456789abc",
        profile_name="Empty profile",
        profile_revision=1,
        profile_definition=FleetProfileDefinition(name="Empty profile"),
        resolved_assignments=[],
        admission_decisions=[],
        assessments=[],
        preparation_decisions=[],
        effects=FleetProfileEffects(runs=[], installations=[], superseded=[]),
        reasons=[],
        scope=FleetProfileScopePreview(node_ids=[]),
        steps=[],
        summary=FleetProfilePlanSummary(
            already_correct=0,
            blockers=0,
            builds=0,
            distributions=0,
            installs=0,
            placements=0,
            starts=0,
            stops=0,
            uninstalls=0,
        ),
    ).to_dict()


def test_cli_profile_preview_uses_the_real_bodyless_request_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    preview = _profile_preview_response()
    observed: list[urllib.request.Request] = []

    def opener(request, *, timeout: float):
        observed.append(request)
        return _Response(200, preview)

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    status = cli.main(
        ("--profile", "7", "profile", "load", "--review", "--json"),
        control_client=client,
    )
    assert status == 0, capsys.readouterr().out
    assert json.loads(capsys.readouterr().out)["allowed"] is True
    assert len(observed) == 1
    assert observed[0].get_method() == "POST"
    assert observed[0].full_url == "https://forge.example.test/api/profile/7/preview"
    assert observed[0].data is None


def test_cli_profile_endpoint_uses_generated_scoped_endpoint_client(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    endpoint_view = {
        "number": 7,
        "profile_id": "11111111-1111-4111-8111-111111111111",
        "application_id": "22222222-2222-4222-8222-222222222222",
        "application_state": "succeeded",
        "observed_at": "2026-09-23T12:59:31Z",
        "assignments": [
            {
                "assignment_id": "33333333-3333-4333-8333-333333333333",
                "recipe_title": "Example Model",
                "desired_state": "running",
                "alias": "studio-chat",
                "state": "published",
                "endpoint": {
                    "alias": "studio-chat",
                    "api_base": "https://vonk-forge.example.ts.net/v1",
                    "backend_api_base": "http://10.0.0.10:8000/v1",
                    "generation": 8,
                    "node_id": "spk_" + "a" * 32,
                    "observed_at": "2026-09-23T12:59:30Z",
                    "plan_digest": "a" * 64,
                },
            }
        ],
    }
    observed: list[urllib.request.Request] = []

    def opener(request, *, timeout: float):
        observed.append(request)
        return _Response(200, endpoint_view)

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    status = cli.main(
        ("--profile", "7", "profile", "endpoint", "studio-chat", "--json"),
        control_client=client,
    )
    output = capsys.readouterr().out

    assert status == 0
    assert json.loads(output)["assignments"][0]["endpoint"]["generation"] == 8
    assert "private-token" not in output
    assert len(observed) == 1
    assert observed[0].get_method() == "GET"
    assert (
        observed[0].full_url
        == "https://forge.example.test/api/profile/7/endpoints?alias=studio-chat"
    )
    assert observed[0].data is None


def test_raw_request_encodes_bounded_query_parameters(tmp_path: Path) -> None:
    observed: list[object] = []

    def opener(request, *, timeout: float):
        observed.extend((request, timeout))
        return _Response(
            200,
            {"operations": [], "total": 0, "next_cursor": None},
        )

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )

    result = client.request(
        "GET",
        "/api/operations",
        query={"cursor": "next page", "state": "waiting-for-operator"},
    )

    assert result == {
        "operations": [],
        "total": 0,
        "next_cursor": None,
    }
    request = observed[0]
    assert isinstance(request, urllib.request.Request)
    assert request.full_url == (
        "https://forge.example.test/api/operations?cursor=next+page&state=waiting-for-operator"
    )
    assert request.get_header("Authorization") == "Bearer private-token"


@pytest.mark.parametrize("retry_seconds", [7, 120])
def test_raw_request_preserves_typed_bounded_api_errors(
    tmp_path: Path, retry_seconds: int
) -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["Retry-After"] = str(retry_seconds)
    body = io.BytesIO(json.dumps({"detail": "bad token private-token"}).encode())

    def opener(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://forge.example.test/api/fleet",
            401,
            "Unauthorized",
            headers,
            body,
        )

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )

    with _not_adopted():
        client.request("GET", "/api/fleet")
    _fresh_request(client)


@pytest.mark.parametrize(
    "problem",
    [{"detail": 7}, {"detail": "bad", "unexpected": True}],
)
def test_raw_request_rejects_malformed_typed_errors(
    tmp_path: Path, problem: dict[str, object]
) -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["X-Request-ID"] = "malformed-error-fixture"
    body = io.BytesIO(json.dumps(problem).encode())

    def opener(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://forge.example.test/api/fleet",
            401,
            "Unauthorized",
            headers,
            body,
        )

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )

    with _not_adopted():
        client.request("GET", "/api/fleet")
    _fresh_request(client)


def test_raw_request_rejects_error_fields_outside_openapi_contract(
    tmp_path: Path,
) -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    body = io.BytesIO(
        json.dumps(
            {
                "code": "model_cache.auth_required",
                "detail": "Hugging Face access is required",
                "recovery_actions": ["open_model_access", "check_access_and_resume"],
                "required_bytes": 200,
                "free_bytes": 100,
                "shortfall_bytes": 100,
                "retry_time": "2026-09-06T13:05:00Z",
                "preserved": "12 MiB of verified bytes",
            }
        ).encode()
    )

    def opener(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://forge.example.test/api/model/qwen-code/download",
            403,
            "Forbidden",
            headers,
            body,
        )

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )

    with _not_adopted():
        client.request(
            "POST",
            "/api/model/qwen-code/download",
            {
                "request_key": "11111111-1111-4111-8111-111111111111",
            },
        )
    _fresh_request(client)


def test_request_validates_canonical_route_models(
    tmp_path: Path,
) -> None:
    capabilities = {
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
        "/api/artifact-jobs/capabilities",
    )
    assert result == capabilities


def test_request_rejects_undocumented_no_content_status(tmp_path: Path) -> None:
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _Response(204, None),
    )

    with _not_adopted():
        client.request("GET", "/api/artifact-jobs/capabilities")
    _fresh_request(client)


def test_request_rejects_response_outside_canonical_route_model(tmp_path: Path) -> None:
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _Response(
            200,
            {"storage": {}, "transport": {}},
        ),
    )

    with _not_adopted():
        client.request(
            "GET",
            "/api/artifact-jobs/capabilities",
        )
    _fresh_request(client)


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

    with _not_adopted():
        client.request(
            "GET",
            "/api/artifact-jobs/capabilities",
        )
    _fresh_request(client)


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

    with _not_adopted():
        client.request(
            "POST",
            "/api/artifact-jobs/job-1/cancel",
            payload,
        )
    _fresh_request(client)


def test_request_rejects_undocumented_success_status(tmp_path: Path) -> None:
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _Response(
            299,
            {
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
            },
        ),
    )

    with _not_adopted():
        client.request("GET", "/api/artifact-jobs/capabilities")
    _fresh_request(client)


def test_request_rejects_route_missing_from_bundled_openapi(tmp_path: Path) -> None:
    client = ControlClient("https://forge.example.test", _token(tmp_path))

    with _not_adopted():
        client.request("GET", "/api/retired-route")
    _fresh_request(client)


def test_request_rejects_json_body_on_binary_route(tmp_path: Path) -> None:
    client = ControlClient("https://forge.example.test", _token(tmp_path))

    with _not_adopted():
        client.request(
            "PUT",
            "/api/artifact-jobs/job-1/inputs/prompt.txt",
            {"content": "not bytes"},
        )
    _fresh_request(client)


def _fleet_transfer_peer(payload: dict[str, object]) -> ObservationHTTPPeer:
    """Real producer envelope; corruption changes only its encoded document."""
    valid = {
        "authority_revision": "a" * 64,
        "event_cursor": 0,
        "generated_at": "2026-09-07T00:00:00+00:00",
        "nodes": [],
    }
    response = observation_response(
        OwnedFleetSnapshot.model_validate_json(json.dumps(valid), strict=True),
        resource="fleet",
    )

    async def collect() -> bytes:
        parts: list[bytes] = []
        async for part in response.body_iterator:
            parts.append(part.encode() if isinstance(part, str) else bytes(part))
        return b"".join(parts)

    body = asyncio.run(collect())
    if payload != valid:
        # Preserve a canonical valid transfer identity, ordering, encoding and
        # complete receipt so malformed Fleet data reaches the domain validator.
        start = ObservationTransferStart.model_validate_json(
            body.splitlines()[0], strict=True
        )
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        chunk = ObservationTransferChunk(
            type="chunk",
            transfer_id=start.transfer_id,
            ordinal=0,
            data=base64.b64encode(raw).decode("ascii"),
        )
        complete = ObservationTransferComplete(
            type="complete",
            transfer_id=start.transfer_id,
            chunks=1,
            bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
        body = b"".join(_record(record) for record in (start, chunk, complete))
    return ObservationHTTPPeer(
        httpx2.Response(
            200,
            content=body,
            headers={"Content-Type": OBSERVATION_MEDIA_TYPE},
            request=httpx2.Request("GET", "https://forge.example.test/api/fleet"),
        )
    )


def test_generated_transport_uses_raw_openapi_contract_before_attrs_parser(
    tmp_path: Path,
) -> None:
    valid = {
        "authority_revision": "a" * 64,
        "event_cursor": 0,
        "generated_at": "2026-09-07T00:00:00+00:00",
        "nodes": [],
    }
    client = ControlClient(
        "https://forge.example.test",
        _token(tmp_path),
        opener=lambda *_args, **_kwargs: _fleet_transfer_peer(valid),
    )

    result = client.fleet()

    assert result.to_dict() == valid


@pytest.mark.parametrize(
    "payload",
    [
        {
            "authority_revision": 7,
            "event_cursor": 0,
            "generated_at": "2026-09-07T00:00:00Z",
            "nodes": [],
        },
        {
            "authority_revision": "a" * 64,
            "event_cursor": 0,
            "generated_at": "2026-09-07T00:00:00Z",
            "nodes": [],
            "unexpected": "schema-private-fixture-value",
        },
    ],
)
def test_generated_transport_rejects_malformed_raw_response(
    tmp_path: Path, payload: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cluster_profiles.generated_control.models.fleet_snapshot import FleetSnapshot

    peers: list[ObservationHTTPPeer] = []
    requests: list[urllib.request.Request] = []
    attrs_calls: list[object] = []

    def opener(
        request: urllib.request.Request, *, timeout: float
    ) -> ObservationHTTPPeer:
        assert timeout > 0 and request.get_method() == "GET"
        requests.append(request)
        peer = _fleet_transfer_peer(payload)
        peer.headers["X-Request-ID"] = "canonical-schema-fixture"
        peers.append(peer)
        return peer

    def parse_partial(document: object) -> FleetSnapshot:
        attrs_calls.append(document)
        raise AssertionError("malformed full observation reached attrs adoption")

    monkeypatch.setattr(FleetSnapshot, "from_dict", parse_partial)
    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )

    with _not_adopted() as failed:
        client.fleet()
    assert "schema-private-fixture-value" not in str(failed[0])
    assert len(requests) > 1 and not attrs_calls
    assert all(peer._body.closed for peer in peers)
    monkeypatch.undo()
    valid = {
        "authority_revision": "a" * 64,
        "event_cursor": 0,
        "generated_at": "2026-09-07T00:00:00+00:00",
        "nodes": [],
    }
    client._opener = lambda *_args, **_kwargs: _fleet_transfer_peer(valid)
    assert client.fleet().to_dict() == valid


def test_observation_callback_hides_arbitrary_validation_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cluster_profiles.control_client import client as control_client

    valid = {
        "authority_revision": "a" * 64,
        "event_cursor": 0,
        "generated_at": "2026-09-07T00:00:00Z",
        "nodes": [],
    }
    peer = _fleet_transfer_peer(valid)
    peer.headers["X-Request-ID"] = "raw-validation-fixture"
    requests: list[urllib.request.Request] = []

    def opener(
        request: urllib.request.Request, *, timeout: float
    ) -> ObservationHTTPPeer:
        assert timeout > 0 and request.get_method() == "GET"
        requests.append(request)
        return _fleet_transfer_peer(valid)

    def unsafe_validator(_component: str, _document: object) -> dict[str, object]:
        raise ValueError("private arbitrary parser value must not be exported")

    original_validator = control_client.validate_control_document
    monkeypatch.setattr(control_client, "validate_control_document", unsafe_validator)
    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    with _not_adopted() as failed:
        client.fleet()
    assert "private arbitrary parser value" not in str(failed[0])
    assert len(requests) > 1
    monkeypatch.setattr(control_client, "validate_control_document", original_validator)
    assert client.fleet().to_dict()["nodes"] == []


def test_generated_transport_rejects_malformed_request_before_network() -> None:
    class _CountingTransport(httpx2.BaseTransport):
        calls = 0

        def handle_request(self, _request: httpx2.Request) -> httpx2.Response:
            self.calls += 1
            raise AssertionError("malformed request reached the network")

    underlying = _CountingTransport()
    transport = _RecordingTransport(underlying)
    request = httpx2.Request(
        "POST",
        "https://forge.example.test/api/model/qwen-code/download",
        headers={"Content-Type": "application/json"},
        content=json.dumps(
            {
                "request_key": 7,
            }
        ).encode(),
    )

    with _not_adopted():
        transport.handle_request(request)

    assert underlying.calls == 0


def test_generated_transport_rejects_malformed_typed_error(tmp_path: Path) -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    body = io.BytesIO(json.dumps({"detail": 7}).encode())

    def opener(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://forge.example.test/api/fleet",
            401,
            "Unauthorized",
            headers,
            body,
        )

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )

    with _not_adopted():
        client.fleet()
    _fresh_request(client)


def test_openapi_validation_is_safe_for_concurrent_requests(
    tmp_path: Path,
) -> None:
    capabilities = {
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
                    "/api/artifact-jobs/capabilities",
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
        "/api/artifact-jobs/job-1/inputs/prompt.txt",
        source,
        media_type="text/plain",
        expected_sha256=digest,
        expected_size=len(content),
    )

    assert result["preparation"] == "draft"
    assert observed == [
        "https://forge.example.test/api/artifact-jobs/job-1/inputs/prompt.txt",
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
        f"/api/artifact-jobs/job-1/results/result.png/{digest}",
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

    with _not_adopted():
        client.download_file(
            f"/api/artifact-jobs/job-1/results/result.png/{expected}",
            destination,
            media_type="image/png",
            expected_sha256=expected,
            expected_size=len(content),
            overwrite=False,
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".result.png.*.download")) == []
    _fresh_request(client)


def test_artifact_output_download_preserves_an_existing_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "result.png"
    destination.write_bytes(b"operator data")
    opened = False

    def opener(*_args, **_kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("existing destination must be refused before transfer")

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )

    with _not_adopted():
        client.download_file(
            "/api/artifact-jobs/12345678-1234-4123-8123-123456789abc/results/result.png/"
            + hashlib.sha256(b"new output").hexdigest(),
            destination,
            media_type="image/png",
            expected_sha256=hashlib.sha256(b"new output").hexdigest(),
            expected_size=len(b"new output"),
            overwrite=False,
        )

    assert not opened
    assert destination.read_bytes() == b"operator data"
    assert list(tmp_path.glob(".result.png.*.download")) == []
    _fresh_request(client)


def test_artifact_output_download_cleans_temporary_file_after_disk_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cluster_profiles.control_client import transfers as control_client

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

    original_link = control_client.os.link

    def disk_full(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(control_client.os, "link", disk_full)

    with _not_adopted():
        client.download_file(
            f"/api/artifact-jobs/12345678-1234-4123-8123-123456789abc/results/result.png/{digest}",
            destination,
            media_type="image/png",
            expected_sha256=digest,
            expected_size=len(content),
            overwrite=False,
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".result.png.*.download")) == []
    control_client.os.link = original_link
    client.download_file(
        f"/api/artifact-jobs/12345678-1234-4123-8123-123456789abc/results/result.png/{digest}",
        destination,
        media_type="image/png",
        expected_sha256=digest,
        expected_size=len(content),
        overwrite=False,
    )
    assert destination.read_bytes() == content
    assert list(tmp_path.glob(".result.png.*.download")) == []


def _job_receipt(job_id: str, state: str):
    from cluster_profiles.generated_control.models.job_detail_response import (
        JobDetailResponse,
    )

    return JobDetailResponse(
        authority_revision="a" * 64,
        current_attempt=1,
        id=job_id,
        kind="run",
        operation_total=0,
        operations=[],
        progress=None,
        state=state,
        target_total=0,
        targets=[],
        status_reason=None,
    ).to_dict()


@pytest.mark.parametrize("verified_first", [False, True])
@pytest.mark.parametrize(
    "fault", ["malformed", "oversized", "unavailable", "lost", "identity"]
)
def test_wait_initial_and_later_unknown_share_budget_and_fresh_job_is_admitted(
    tmp_path: Path,
    observation_clock: list[float],
    fault: str,
    verified_first: bool,
) -> None:
    from cluster_profiles.cli_states_generated import RUNNING, SUCCEEDED

    job_id = "12345678-1234-4123-8123-123456789abc"
    calls: list[tuple[str, float]] = []
    phase = [0 if verified_first else 1]

    def opener(request, *, timeout):
        calls.append((request.full_url, timeout))
        if phase[0] == 0:
            phase[0] = 1
            return _Response(200, _job_receipt(job_id, RUNNING))
        if phase[0] == 2:
            return _Response(200, _job_receipt(job_id, SUCCEEDED))
        if fault == "lost":
            raise OSError("connection lost")
        if fault == "identity":
            return _Response(
                200, _job_receipt("22345678-1234-4123-8123-123456789abc", SUCCEEDED)
            )
        if fault == "oversized":
            from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

            return _Response(200, {"padding": "x" * MAX_CONTROL_DOCUMENT_BYTES})
        return _Response(503 if fault == "unavailable" else 200, {"broken": True})

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    started = observation_clock[0]
    with _not_adopted() as ended:
        client.wait_job(job_id, timeout=1, interval=0.1)
    assert observation_clock[0] - started == pytest.approx(1)
    ending = cast(ControlTimeout, ended[0])
    if verified_first:
        assert ending.job is not None
        assert ending.job.id == job_id
        assert ending.job.state == RUNNING
    else:
        assert ending.job is None
    assert ending.job_id == job_id
    assert len(calls) > 2
    assert all(
        urllib.parse.urlsplit(url).path == "/api/jobs/" + job_id for url, _ in calls
    )
    assert all(0 < timeout <= 1 for _, timeout in calls)
    assert calls[-1][1] < calls[0][1]
    phase[0] = 2
    assert client.wait_job(job_id, timeout=1, interval=0.1).state == SUCCEEDED


@pytest.mark.parametrize("first_status", [200, 404, 409, 422, 503])
def test_unreadable_first_reply_reobserves_exact_read_and_recovers(
    tmp_path: Path,
    first_status: int,
) -> None:
    calls: list[urllib.request.Request] = []
    job_id = "12345678-1234-4123-8123-123456789abc"
    path = "/api/artifact-jobs/" + job_id

    def opener(request, *, timeout):
        calls.append(request)
        return (
            _Response(first_status, {"broken": True})
            if len(calls) == 1
            else _Response(200, _artifact_job_response())
        )

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    assert client.request("GET", path)["id"] == job_id
    assert len(calls) == 2
    assert all(
        request.get_method() == "GET" and request.full_url.endswith(path)
        for request in calls
    )
    assert client.request("GET", path)["id"] == job_id


@pytest.mark.parametrize("method", ["GET", "POST", "generated", "stream"])
@pytest.mark.parametrize("status", [401, 403])
def test_real_denial_never_reads_untrusted_body_or_replays_mutation(
    tmp_path: Path,
    status: int,
    method: str,
) -> None:
    calls: list[urllib.request.Request] = []
    denied = [True]
    from vonk_agent_protocol.lifecycle_vocabulary import SecurityRefusalReason

    code = (
        SecurityRefusalReason.CONTROLLER_AUTHENTICATION_REQUIRED
        if status == 401
        else SecurityRefusalReason.CONTROLLER_REQUEST_REJECTED
    )

    class Denial(_Response):
        def __init__(self, status: int, payload: object | None) -> None:
            super().__init__(status, payload)
            self.headers["X-Vonk-Error-Code"] = code

        def read(self, maximum: int) -> bytes:
            raise AssertionError("authorization denial waited for a body")

    def opener(request, *, timeout):
        calls.append(request)
        return (
            Denial(status, None)
            if denied[0]
            else _Response(200, _artifact_job_response())
        )

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    path = "/api/artifact-jobs/12345678-1234-4123-8123-123456789abc"
    with _not_adopted() as ended:
        if method == "POST":
            client.request(
                "POST",
                "/api/model/chosen/download",
                {"request_key": "11111111-1111-4111-8111-111111111111"},
            )
        elif method == "generated":
            client.job("12345678-1234-4123-8123-123456789abc")
        elif method == "stream":
            client.request("GET", "/api/fleet")
        else:
            client.request("GET", path)
    error = cast(ControlClientError, ended[0])
    assert error.context is not None
    assert error.context.http_status == status
    assert len(calls) == 1
    denied[0] = False
    assert client.request("GET", path)["id"] == _artifact_job_response()["id"]
    assert len(calls) == 2


@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf")])
@pytest.mark.parametrize("option", ["timeout", "interval"])
def test_wait_invalid_options_have_no_effect_and_valid_fresh_read_works(
    tmp_path: Path,
    invalid: float,
    option: str,
) -> None:
    from cluster_profiles.cli_states_generated import SUCCEEDED

    calls: list[urllib.request.Request] = []
    job_id = "12345678-1234-4123-8123-123456789abc"

    def opener(request, *, timeout):
        calls.append(request)
        return _Response(200, _job_receipt(job_id, SUCCEEDED))

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    with _not_adopted():
        client.wait_job(
            job_id,
            timeout=invalid if option == "timeout" else 1,
            interval=invalid if option == "interval" else 0.1,
        )
    assert not calls
    assert client.wait_job(job_id, timeout=1, interval=0.1).id == job_id
    assert len(calls) == 1


def test_unreadable_bundled_schema_is_reobserved_before_any_network_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cluster_profiles.control_client import schema

    original = schema.files
    reads = []
    network = []

    class Unreadable:
        def joinpath(self, _name):
            return self

        def read_text(self):
            reads.append(None)
            raise OSError("temporary local read failure")

    def files(package):
        return Unreadable() if not reads else original(package)

    schema._control_openapi.cache_clear()
    schema._control_validator.cache_clear()
    schema._operation.cache_clear()
    monkeypatch.setattr(schema, "files", files)

    def opener(request, *, timeout):
        assert reads, "network effect preceded contract recovery"
        network.append(request)
        return _Response(200, _artifact_job_response())

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    path = "/api/artifact-jobs/12345678-1234-4123-8123-123456789abc"
    assert client.request("GET", path)["id"] == _artifact_job_response()["id"]
    assert len(reads) == 1 and len(network) == 1
    assert client.request("GET", path)["id"] == _artifact_job_response()["id"]
    assert len(network) == 2


def _fresh_request(client: ControlClient) -> None:
    client._opener = lambda *_args, **_kwargs: _Response(200, _artifact_job_response())
    path = "/api/artifact-jobs/12345678-1234-4123-8123-123456789abc"
    assert client.request("GET", path)["id"] == _artifact_job_response()["id"]


def test_generated_request_validation_cannot_spend_the_budget_then_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observation_clock: list[float],
) -> None:
    from cluster_profiles.cli_states_generated import SUCCEEDED
    from cluster_profiles.control_client import schema

    original = schema._request_contract
    calls = []
    job_id = "12345678-1234-4123-8123-123456789abc"

    def slow(path, method, body):
        original(path, method, body)
        observation_clock[0] += 2

    def opener(request, *, timeout):
        calls.append(request)
        return _Response(200, _job_receipt(job_id, SUCCEEDED))

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    monkeypatch.setattr(schema, "_request_contract", slow)
    with _not_adopted():
        client.job(job_id, timeout_seconds=1)
    assert not calls
    monkeypatch.setattr(schema, "_request_contract", original)
    assert client.job(job_id, timeout_seconds=1).id == job_id
    assert len(calls) == 1


def test_damaged_cached_response_schema_is_discarded_before_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copy import deepcopy

    from cluster_profiles.control_client import schema

    schema._control_openapi.cache_clear()
    good = json.loads(json.dumps(schema._control_openapi()))
    damaged = deepcopy(good)
    damaged["paths"]["/api/artifact-jobs/{job_id}"]["get"]["responses"]["200"][
        "content"
    ]["application/json"].pop("schema")
    reads = []
    calls = []

    class Document:
        def joinpath(self, _name):
            return self

        def read_text(self):
            reads.append(None)
            return json.dumps(damaged if len(reads) == 1 else good)

    schema._control_openapi.cache_clear()
    schema._control_validator.cache_clear()
    schema._operation.cache_clear()
    monkeypatch.setattr(schema, "files", lambda _package: Document())

    def opener(request, *, timeout):
        calls.append(request)
        return _Response(
            200, {"broken": True} if len(calls) == 1 else _artifact_job_response()
        )

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    path = "/api/artifact-jobs/12345678-1234-4123-8123-123456789abc"
    assert client.request("GET", path)["id"] == _artifact_job_response()["id"]
    assert len(reads) == 2 and len(calls) == 2
    assert client.request("GET", path)["id"] == _artifact_job_response()["id"]


@pytest.mark.parametrize(
    "binding",
    [None, "a" * 64, "", "invalid", "A" * 64, "transport", "missing", "unavailable"],
)
@pytest.mark.parametrize("fault_count", [1, 3])
def test_preview_recovery_uses_canonical_transport_before_any_load(
    tmp_path, capsys, binding, fault_count
):
    from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
    from vonk_control.fleet_profile_contract import (
        FleetProfileApplicationProgress,
        FleetProfileApplicationResult,
        FleetProfileApplicationView,
    )
    from vonk_control.strict_json import serialize_json_value

    from cluster_profiles.controller_cli.profile_load import (
        _review_and_submit_profile_load,
    )

    key = "11111111-1111-4111-8111-111111111111"
    application = FleetProfileApplicationView(
        id="33333333-3333-4333-8333-333333333333",
        request_key=key,
        profile_id="12345678-1234-4123-8123-123456789abc",
        profile_digest="b" * 64,
        plan_digest="a" * 64,
        state=LifecycleState.SUCCEEDED,
        current_step=0,
        total_steps=0,
        current_operation_id=None,
        status_reason=None,
        progress=FleetProfileApplicationProgress(),
        result=FleetProfileApplicationResult(changed=False, completed_steps=0),
        created_at=datetime(2026, 10, 8, tzinfo=UTC),
        updated_at=datetime(2026, 10, 8, tzinfo=UTC),
    )
    calls = []
    repair = [False]
    previews = [0]

    def opener(request, *, timeout):
        calls.append(request)
        if request.full_url.endswith("/preview"):
            previews[0] += 1
            preview = _profile_preview_response()
            if previews[0] > fault_count:
                repair[0] = True
            if not repair[0]:
                if binding == "transport":
                    raise urllib.error.URLError("peer connection lost")
                if binding in {"missing", "unavailable"}:
                    return _Response(
                        404 if binding == "missing" else 503,
                        {"detail": "Projection unavailable"},
                    )
            if binding is None and not repair[0]:
                preview.pop("effects_digest", None)
            else:
                preview["effects_digest"] = "a" * 64 if repair[0] else binding
            return _Response(200, preview)
        assert request.full_url.endswith("/load")
        return _Response(202, serialize_json_value(application))

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    args = cli._parser().parse_args(("--json", "profile", "load", "--yes", "--detach"))

    def load():
        return _review_and_submit_profile_load(
            client, 7, args, lambda: key, question="Load?", review_when_confirmed=True
        )

    result = load()
    invalid = binding != "a" * 64
    if invalid and fault_count == 3:
        assert args.observation.status == "timed_out"
        assert result == {}
        assert all(request.full_url.endswith("/preview") for request in calls)
        repair[0] = True
        result = load()
    assert result["id"] == application.id
    payload = json.loads(calls[-1].data)
    assert payload == ({"request_key": key, "review": {"effects_digest": "a" * 64}})
    capsys.readouterr()


@pytest.mark.parametrize("exhaust_budget", [False, True])
def test_whole_observation_unavailable_keeps_bytes_unconsumed_and_recovers(
    tmp_path, observation_clock, exhaust_budget
):
    from vonk_agent_protocol.reason_codes import ProjectionCode
    from vonk_control.observation_transfer import ObservationTransferError

    from cluster_profiles.controller_cli.observation import _poll_path

    snapshot = {
        "authority_revision": "a" * 64,
        "event_cursor": 0,
        "generated_at": "2026-09-07T00:00:00+00:00",
        "nodes": [],
    }
    valid_peer = _fleet_transfer_peer(snapshot)
    records = valid_peer._body.getvalue().splitlines(keepends=True)
    start = ObservationTransferStart.model_validate_json(records[0])
    incomplete = b"".join(records[:-1]) + _record(
        ObservationTransferError(
            type="error",
            transfer_id=start.transfer_id,
            reason_code=ProjectionCode.OBSERVATION_TRANSFER_UNAVAILABLE,
            detail="Projection unavailable",
        )
    )
    calls = []
    repaired = [False]

    def opener(request, *, timeout):
        calls.append(request.get_method())
        if not repaired[0]:
            if not exhaust_budget:
                repaired[0] = True
            return _StreamResponse(
                incomplete,
                media_type=OBSERVATION_MEDIA_TYPE,
                sha256=hashlib.sha256(incomplete).hexdigest(),
            )
        return _fleet_transfer_peer(snapshot)

    client = ControlClient(
        "https://forge.example.test", _token(tmp_path), opener=opener
    )
    args = cli._parser().parse_args(("fleet", "--json"))
    started = observation_clock[0]

    def observe():
        return _poll_path(
            client,
            "/api/fleet",
            {},
            args,
            fetch_initial=True,
            attempts=3,
            terminal=lambda _: True,
        )

    result = observe()
    if exhaust_budget:
        assert result == {}
        assert args.observation.status == "timed_out"
        assert args.observation.reconnect_command
        assert observation_clock[0] - started == pytest.approx(
            args.observation.timeout_seconds
        )
        repaired[0] = True
        result = observe()
    from vonk_control.strict_json import serialize_json_value

    assert result == serialize_json_value(
        OwnedFleetSnapshot.model_validate_json(json.dumps(snapshot))
    )
    assert args.observation.status == "complete"
    assert len(calls) > 1
    assert all(method == "GET" for method in calls)
