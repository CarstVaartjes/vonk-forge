import io
import json
import time
import urllib.error
import urllib.response
from email.message import Message
from pathlib import Path

import pytest

from cluster_profiles import control_client
from cluster_profiles.cli import main
from cluster_profiles.control_client import (
    ControlClient,
    ControlClientError,
    PackagePlanResponse,
)
from cluster_profiles.generated_control.models.agents_response import AgentsResponse
from cluster_profiles.generated_control.models.endpoint_response import EndpointResponse
from cluster_profiles.generated_control.models.fleet_status_response import (
    FleetStatusResponse,
)
from cluster_profiles.generated_control.models.job_detail_response import (
    JobDetailResponse,
)

COMMIT = "a" * 40
PLAN_DIGEST = "b" * 64
FLEET_EVIDENCE_DIGEST = "c" * 64
JOB_ID = "11111111-1111-4111-8111-111111111111"


class Response:
    def __init__(self, payload, status=200, *, headers=None, raw=False):
        self._content = payload if raw else json.dumps(payload).encode()
        self.status = status
        self.headers = {"content-type": "application/json", **(headers or {})}

    def read(self, size=-1):
        if size < 0:
            return self._content
        value, self._content = self._content[:size], self._content[size:]
        return value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class RedirectingProtocolHandler(urllib.request.BaseHandler):
    handler_order = 100

    def __init__(self, location: str) -> None:
        self.location = location
        self.calls: list[urllib.request.Request] = []

    def default_open(self, request: urllib.request.Request):
        self.calls.append(request)
        headers = Message()
        headers["content-type"] = "application/json"
        if len(self.calls) == 1:
            headers["location"] = self.location
            status = 302
            body = b'{"detail":"redirected"}'
        else:
            status = 200
            body = b'{"ok":true}'
        response = urllib.response.addinfourl(
            io.BytesIO(body), headers, request.full_url, status
        )
        response.msg = "Found" if status == 302 else "OK"
        return response


def generated_client(tmp_path: Path, opener) -> ControlClient:
    return ControlClient(
        "https://control.invalid",
        token_file(tmp_path),
        opener=opener,
    )


def token_file(tmp_path: Path) -> Path:
    token = tmp_path / "token"
    token.write_text("signed-token\n")
    token.chmod(0o600)
    return token


def job_payload(state: str, reason: str | None = None) -> dict[str, object]:
    return {
        "base_commit": COMMIT,
        "current_attempt": 1,
        "id": JOB_ID,
        "kind": "reconcile",
        "operation_next_cursor": None,
        "operation_total": 0,
        "operations": [],
        "progress": {"completed": 0, "failed": 0, "running": 0, "total": 0},
        "reconciliation_id": "22222222-2222-4222-8222-222222222222",
        "state": state,
        "status_reason": reason,
        "target_next_cursor": None,
        "target_total": 0,
        "targets": [],
    }


def test_client_reads_token_file_and_sends_canonical_proposal(tmp_path: Path) -> None:
    token = token_file(tmp_path)
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return Response({"digest": "abc", "patch": "diff"})

    client = ControlClient("https://control.invalid", token, opener=opener)
    result = client.create_proposal(
        {
            "base_commit": "base",
            "changes": [
                {"path": "inventory/topology.json", "document": {"schema_version": 1}}
            ],
        }
    )
    request = calls[0][0]
    assert request.full_url == "https://control.invalid/api/v1/proposals"
    assert request.headers["Authorization"] == "Bearer signed-token"
    assert json.loads(request.data) == {
        "base_commit": "base",
        "changes": [
            {"document": {"schema_version": 1}, "path": "inventory/topology.json"}
        ],
    }
    assert result == {"digest": "abc", "patch": "diff"}


def test_operational_methods_use_generated_models_and_exact_routes(
    tmp_path: Path,
) -> None:
    responses = iter(
        [
            Response({"commit": COMMIT, "nodes": [], "evidence_digest": FLEET_EVIDENCE_DIGEST}),
            Response(job_payload("running")),
            Response(
                {
                    "alias": "model-a",
                    "api_base": "https://model.invalid/v1",
                    "expires_at": "2026-08-05T13:00:00Z",
                    "generation": 7,
                    "node_id": "spk_" + "c" * 32,
                    "observed_at": "2026-08-05T12:00:00Z",
                    "plan_digest": PLAN_DIGEST,
                    "state": "active",
                }
            ),
            Response({"agents": []}),
        ]
    )
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return next(responses)

    client = generated_client(tmp_path, opener)

    assert isinstance(client.nodes(), FleetStatusResponse)
    assert isinstance(client.job(JOB_ID), JobDetailResponse)
    assert isinstance(client.endpoint("model-a"), EndpointResponse)
    assert isinstance(client.agents(), AgentsResponse)

    assert [(call[0].method, call[0].full_url) for call in calls] == [
        ("GET", "https://control.invalid/api/v1/nodes/status"),
        ("GET", f"https://control.invalid/api/v1/jobs/{JOB_ID}?limit=20"),
        ("GET", "https://control.invalid/api/v1/endpoints/model-a"),
        ("GET", "https://control.invalid/api/v1/agents"),
    ]


def test_supplied_opener_is_used_by_generated_and_preserved_methods(
    tmp_path: Path, monkeypatch
) -> None:
    responses = iter(
        [
            Response({"commit": COMMIT, "nodes": [], "evidence_digest": FLEET_EVIDENCE_DIGEST}),
            Response({"digest": "abc", "patch": "diff"}),
        ]
    )
    calls: list[urllib.request.Request] = []

    def opener(request, timeout):
        calls.append(request)
        return next(responses)

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    def reject_fallback_boundary(*handlers):
        raise AssertionError("caller-supplied opener was bypassed")

    monkeypatch.setattr(urllib.request, "build_opener", reject_fallback_boundary)

    assert isinstance(client.nodes(), FleetStatusResponse)
    assert client.create_proposal({"base_commit": COMMIT, "changes": []}) == {
        "digest": "abc",
        "patch": "diff",
    }
    assert [request.full_url for request in calls] == [
        "https://control.invalid/api/v1/nodes/status",
        "https://control.invalid/api/v1/proposals",
    ]


@pytest.mark.parametrize(
    ("status", "exception_name"),
    [
        (401, "ControlUnauthorized"),
        (403, "ControlForbidden"),
        (409, "ControlConflict"),
        (503, "ControlUnavailable"),
    ],
)
def test_control_statuses_raise_typed_failures(
    tmp_path: Path, status: int, exception_name: str
) -> None:
    def opener(request, timeout):
        return Response({"detail": "bounded failure"}, status=status)

    client = generated_client(tmp_path, opener)
    expected = getattr(control_client, exception_name)

    with pytest.raises(expected) as caught:
        client.endpoint("model-a")

    assert caught.value.status_code == status
    assert caught.value.detail in {"bounded failure", "control API request failed"}


@pytest.mark.parametrize(
    ("status", "exception_name"),
    [
        (401, "ControlUnauthorized"),
        (403, "ControlForbidden"),
        (404, "ControlNotFound"),
        (409, "ControlConflict"),
        (503, "ControlUnavailable"),
    ],
)
@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"", "application/json"),
        (b"<html>failure</html>", "text/html"),
        (b"{", "application/json"),
        (b'{"wrong":"shape"}', "application/json"),
    ],
    ids=["empty", "html", "malformed-json", "schema-invalid"],
)
def test_http_status_typing_precedes_unusable_error_body_parsing(
    tmp_path: Path,
    status: int,
    exception_name: str,
    body: bytes,
    content_type: str,
) -> None:
    def opener(request, timeout):
        return Response(
            body,
            status=status,
            headers={"content-type": content_type, "retry-after": "99"},
            raw=True,
        )

    client = generated_client(tmp_path, opener)
    expected = getattr(control_client, exception_name)

    with pytest.raises(expected) as caught:
        if status == 404:
            client.job(JOB_ID)
        else:
            client.endpoint("model-a")

    assert caught.value.status_code == status
    assert caught.value.detail == "control API request failed"
    assert caught.value.retry_after_seconds == 30


@pytest.mark.parametrize(
    ("status", "exception_name"),
    [
        (401, "ControlUnauthorized"),
        (403, "ControlForbidden"),
        (404, "ControlNotFound"),
        (409, "ControlConflict"),
        (503, "ControlUnavailable"),
    ],
)
def test_http_status_typing_precedes_recursive_json_failure(
    tmp_path: Path, status: int, exception_name: str
) -> None:
    deeply_nested_json = b"[" * 10_000 + b"0" + b"]" * 10_000

    def opener(request, timeout):
        return Response(
            deeply_nested_json,
            status=status,
            headers={"retry-after": "99"},
            raw=True,
        )

    client = generated_client(tmp_path, opener)
    expected = getattr(control_client, exception_name)

    with pytest.raises(expected) as caught:
        if status == 404:
            client.job(JOB_ID)
        else:
            client.endpoint("model-a")

    assert caught.value.status_code == status
    assert caught.value.detail == "control API request failed"
    assert caught.value.retry_after_seconds == 30


def test_successful_recursive_json_is_reported_as_malformed_response(
    tmp_path: Path,
) -> None:
    deeply_nested_json = b"[" * 10_000 + b"0" + b"]" * 10_000

    def opener(request, timeout):
        return Response(deeply_nested_json, raw=True)

    client = generated_client(tmp_path, opener)

    with pytest.raises(control_client.ControlMalformedResponse, match="nesting"):
        client.endpoint("model-a")


def test_missing_resource_raises_typed_not_found(tmp_path: Path) -> None:
    def opener(request, timeout):
        return Response({"detail": "job not found"}, status=404)

    client = generated_client(tmp_path, opener)

    with pytest.raises(control_client.ControlNotFound) as caught:
        client.job(JOB_ID)

    assert caught.value.status_code == 404
    assert caught.value.detail == "job not found"


def test_malformed_json_raises_typed_response_failure(tmp_path: Path) -> None:
    def opener(request, timeout):
        return Response(b"not-json", raw=True)

    client = generated_client(tmp_path, opener)

    with pytest.raises(control_client.ControlMalformedResponse, match="invalid JSON"):
        client.endpoint("model-a")


def test_malformed_generated_model_raises_typed_response_failure(
    tmp_path: Path,
) -> None:
    def opener(request, timeout):
        return Response({"commit": COMMIT})

    client = generated_client(tmp_path, opener)

    with pytest.raises(control_client.ControlMalformedResponse, match="schema"):
        client.nodes()


def test_oversized_generated_response_is_rejected_before_parsing(
    tmp_path: Path,
) -> None:
    def opener(request, timeout):
        return Response(b"{" + b" " * 1_048_576, raw=True)

    client = generated_client(tmp_path, opener)

    with pytest.raises(control_client.ControlResponseTooLarge, match="safety limit"):
        client.nodes()


def test_generated_response_requires_json_content_type(tmp_path: Path) -> None:
    def opener(request, timeout):
        return Response(
            {"commit": COMMIT, "nodes": [], "evidence_digest": FLEET_EVIDENCE_DIGEST},
            headers={"content-type": "text/html"},
        )

    client = generated_client(tmp_path, opener)

    with pytest.raises(control_client.ControlMalformedResponse, match="content type"):
        client.nodes()


@pytest.mark.parametrize(
    "location",
    ["http://attacker.invalid/steal", "https://other.invalid/steal"],
)
@pytest.mark.parametrize("mutation", ["proposal", "change"])
def test_production_boundary_rejects_mutation_redirect_without_forward_or_replay(
    tmp_path: Path, monkeypatch, location: str, mutation: str
) -> None:
    real_build_opener = urllib.request.build_opener
    protocol_handler = RedirectingProtocolHandler(location)

    def controlled_build_opener(*handlers):
        return real_build_opener(protocol_handler, *handlers)

    monkeypatch.setattr(urllib.request, "_opener", real_build_opener(protocol_handler))
    monkeypatch.setattr(urllib.request, "build_opener", controlled_build_opener)
    client = ControlClient("https://control.invalid", token_file(tmp_path))

    with pytest.raises(ControlClientError):
        if mutation == "proposal":
            client.create_proposal({"base_commit": COMMIT, "changes": []})
        else:
            client.submit_change(PLAN_DIGEST)

    assert len(protocol_handler.calls) == 1
    assert protocol_handler.calls[0].full_url.startswith(
        "https://control.invalid/api/v1/"
    )
    assert protocol_handler.calls[0].headers["Authorization"] == ("Bearer signed-token")


def test_urlopen_http_error_body_keeps_typed_status_mapping(tmp_path: Path) -> None:
    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            503,
            "Service Unavailable",
            {"content-type": "application/json"},
            io.BytesIO(b'{"detail":"try later"}'),
        )

    client = generated_client(tmp_path, opener)

    with pytest.raises(control_client.ControlUnavailable) as caught:
        client.endpoint("model-a")

    assert caught.value.status_code == 503
    assert caught.value.detail == "try later"


@pytest.mark.parametrize(
    ("header", "expected"),
    [("0", 1), ("1", 1), ("17", 17), ("31", 30), ("invalid", None)],
)
def test_retry_after_is_bounded_to_safe_seconds(
    tmp_path: Path, header: str, expected: int | None
) -> None:
    def opener(request, timeout):
        return Response(
            {"detail": "try later"},
            status=503,
            headers={"retry-after": header},
        )

    client = generated_client(tmp_path, opener)

    with pytest.raises(control_client.ControlUnavailable) as caught:
        client.endpoint("model-a")

    assert caught.value.retry_after_seconds == expected


@pytest.mark.parametrize(
    ("remote_text", "forbidden_values"),
    [
        ("Authorization: Bearer signed-token", ("signed-token",)),
        ("upstream rejected signed-token", ("signed-token",)),
        (
            (
                "-----BEGIN CERTIFICATE-----\nCERTIFICATE-BODY\n"
                "-----END CERTIFICATE-----\n"
                "-----BEGIN PRIVATE KEY-----\nPRIVATE-KEY-BODY\n"
                "-----END PRIVATE KEY-----"
            ),
            ("CERTIFICATE-BODY", "PRIVATE-KEY-BODY"),
        ),
        (
            "password=hunter2 credential=https://admin:swordfish@host.invalid",
            ("hunter2", "admin", "swordfish"),
        ),
        (
            "client_certificate=CERTIFICATE-BODY x509=CERTIFICATE-CHAIN",
            ("CERTIFICATE-BODY", "CERTIFICATE-CHAIN"),
        ),
        (
            "certificate=" + "CERTIFICATE-SECRET-" * 300,
            ("CERTIFICATE-SECRET",),
        ),
        ("x" * 5_000, ("x" * 257,)),
    ],
    ids=[
        "bearer",
        "bare-token",
        "pem",
        "credential",
        "certificate-labels",
        "oversized-certificate",
        "oversized",
    ],
)
def test_http_error_detail_is_bounded_and_redacted(
    tmp_path: Path, remote_text: str, forbidden_values: tuple[str, ...]
) -> None:
    def opener(request, timeout):
        return Response(
            {"detail": remote_text},
            status=409,
            headers={"content-type": "application/json"},
        )

    client = generated_client(tmp_path, opener)

    with pytest.raises(control_client.ControlConflict) as caught:
        client.endpoint("model-a")

    assert len(caught.value.detail) <= 256
    for forbidden in forbidden_values:
        assert forbidden not in caught.value.detail
        assert forbidden not in str(caught.value)


@pytest.mark.parametrize(
    ("remote_text", "forbidden_values"),
    [
        ("Bearer signed-token", ("signed-token",)),
        ("upstream rejected signed-token", ("signed-token",)),
        (
            (
                "-----BEGIN CERTIFICATE-----\nCERTIFICATE-BODY\n"
                "-----END CERTIFICATE-----\n"
                "-----BEGIN PRIVATE KEY-----\nPRIVATE-KEY-BODY\n"
                "-----END PRIVATE KEY-----"
            ),
            ("CERTIFICATE-BODY", "PRIVATE-KEY-BODY"),
        ),
        ("api_key=raw-key password=hunter2", ("raw-key", "hunter2")),
        (
            "client_certificate=CERTIFICATE-BODY x509=CERTIFICATE-CHAIN",
            ("CERTIFICATE-BODY", "CERTIFICATE-CHAIN"),
        ),
        (
            "certificate=" + "CERTIFICATE-SECRET-" * 300,
            ("CERTIFICATE-SECRET",),
        ),
        ("y" * 5_000, ("y" * 257,)),
    ],
    ids=[
        "bearer",
        "bare-token",
        "pem",
        "credential",
        "certificate-labels",
        "oversized-certificate",
        "oversized",
    ],
)
def test_terminal_job_reason_is_bounded_and_redacted(
    tmp_path: Path, remote_text: str, forbidden_values: tuple[str, ...]
) -> None:
    def opener(request, timeout):
        return Response(
            job_payload("failed", remote_text),
            headers={"content-type": "application/json"},
        )

    client = generated_client(tmp_path, opener)

    with pytest.raises(control_client.JobFailed) as caught:
        client.wait_job(JOB_ID, timeout=1, interval=0)

    assert caught.value.reason == caught.value.job.status_reason
    assert caught.value.reason is not None
    assert len(caught.value.reason) <= 256
    for forbidden in forbidden_values:
        assert forbidden not in caught.value.reason
        assert forbidden not in str(caught.value)


def test_wait_job_returns_structured_terminal_success(tmp_path: Path) -> None:
    def opener(request, timeout):
        return Response(job_payload("succeeded"))

    client = generated_client(tmp_path, opener)

    result = client.wait_job(JOB_ID, timeout=1, interval=0)

    assert isinstance(result, JobDetailResponse)
    assert result.state == "succeeded"
    assert result.id == JOB_ID


@pytest.mark.parametrize(
    ("state", "exception_name"),
    [
        ("failed", "JobFailed"),
        ("expired", "JobFailed"),
        ("waiting-for-operator", "JobWaitingForOperator"),
    ],
)
def test_wait_job_raises_typed_terminal_failure(
    tmp_path: Path, state: str, exception_name: str
) -> None:
    def opener(request, timeout):
        return Response(job_payload(state, "operator action required"))

    client = generated_client(tmp_path, opener)
    expected = getattr(control_client, exception_name)

    with pytest.raises(expected) as caught:
        client.wait_job(JOB_ID, timeout=1, interval=0)

    assert isinstance(caught.value.job, JobDetailResponse)
    assert caught.value.job.state == state
    assert caught.value.reason == "operator action required"


def test_wait_job_polls_only_get_until_terminal_state(tmp_path: Path) -> None:
    responses = iter(
        [
            Response(job_payload("queued")),
            Response(job_payload("running")),
            Response(job_payload("succeeded")),
        ]
    )
    calls = []

    def opener(request, timeout):
        calls.append(request)
        return next(responses)

    client = generated_client(tmp_path, opener)

    result = client.wait_job(JOB_ID, timeout=1, interval=0)

    assert result.state == "succeeded"
    assert [(request.method, request.full_url) for request in calls] == [
        ("GET", f"https://control.invalid/api/v1/jobs/{JOB_ID}?limit=20"),
        ("GET", f"https://control.invalid/api/v1/jobs/{JOB_ID}?limit=20"),
        ("GET", f"https://control.invalid/api/v1/jobs/{JOB_ID}?limit=20"),
    ]


def test_wait_job_times_out_with_last_observation(tmp_path: Path) -> None:
    calls = 0

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        if calls > 50:
            raise AssertionError("polling ignored its deadline")
        return Response(job_payload("queued"))

    client = generated_client(tmp_path, opener)

    with pytest.raises(control_client.ControlTimeout) as caught:
        client.wait_job(JOB_ID, timeout=0.005, interval=0.001)

    assert caught.value.job is not None
    assert caught.value.job.state == "queued"
    assert 1 <= calls <= 50


@pytest.mark.parametrize(
    ("remote_text", "forbidden_values"),
    [
        ("upstream rejected signed-token", ("signed-token",)),
        (
            (
                "-----BEGIN CERTIFICATE-----\nCERTIFICATE-BODY\n"
                "-----END CERTIFICATE-----\n"
                "-----BEGIN PRIVATE KEY-----\nPRIVATE-KEY-BODY\n"
                "-----END PRIVATE KEY-----"
            ),
            ("CERTIFICATE-BODY", "PRIVATE-KEY-BODY"),
        ),
        ("password = hunter2", ("hunter2",)),
        ("z" * 5_000, ("z" * 257,)),
        ("certificate_pem = CERTIFICATE-PEM-CONTENT", ("CERTIFICATE-PEM-CONTENT",)),
        ("cert_pem=CERT-PEM-CONTENT", ("CERT-PEM-CONTENT",)),
        ("chain_pem : CHAIN-PEM-CONTENT", ("CHAIN-PEM-CONTENT",)),
        ("CeRt_PeM   =   MIXED-CASE-CERTIFICATE", ("MIXED-CASE-CERTIFICATE",)),
    ],
    ids=[
        "bare-token",
        "pem",
        "credential",
        "oversized",
        "certificate-pem",
        "cert-pem",
        "chain-pem",
        "mixed-case-spacing",
    ],
)
def test_wait_job_timeout_stores_safe_bounded_observation(
    tmp_path: Path, remote_text: str, forbidden_values: tuple[str, ...]
) -> None:
    def opener(request, timeout):
        return Response(job_payload("running", remote_text))

    client = generated_client(tmp_path, opener)

    with pytest.raises(control_client.ControlTimeout) as caught:
        client.wait_job(JOB_ID, timeout=0, interval=0)

    assert caught.value.job is not None
    assert caught.value.job.id == JOB_ID
    assert caught.value.job.state == "running"
    assert caught.value.job.base_commit == COMMIT
    assert caught.value.job.current_attempt == 1
    assert caught.value.job.progress.total == 0
    assert caught.value.job.status_reason is not None
    assert len(caught.value.job.status_reason) <= 256
    for forbidden in forbidden_values:
        assert forbidden not in caught.value.job.status_reason
        assert forbidden not in str(caught.value)


@pytest.mark.parametrize(
    ("failure_kind", "cause_type"),
    [
        ("transport", control_client.ControlTransportError),
        ("unavailable", control_client.ControlUnavailable),
    ],
)
def test_wait_job_transient_timeout_stores_safe_bounded_observation(
    tmp_path: Path, monkeypatch, failure_kind: str, cause_type: type[Exception]
) -> None:
    remote_text = (
        "signed-token CERT_PEM = CERTIFICATE-CONTENT password=hunter2 " + "q" * 5_000
    )
    calls = 0

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return Response(job_payload("queued", remote_text))
        if failure_kind == "transport":
            raise urllib.error.URLError("connection reset")
        return Response({"detail": "try later"}, status=503)

    clock = iter([0.0, 0.5, 1.1])
    monkeypatch.setattr(control_client.time, "monotonic", lambda: next(clock))
    client = generated_client(tmp_path, opener)

    with pytest.raises(control_client.ControlTimeout) as caught:
        client.wait_job(JOB_ID, timeout=1, interval=0)

    assert isinstance(caught.value.__cause__, cause_type)
    assert calls == 2
    assert caught.value.job is not None
    assert caught.value.job.id == JOB_ID
    assert caught.value.job.state == "queued"
    assert caught.value.job.progress.total == 0
    assert caught.value.job.status_reason is not None
    assert len(caught.value.job.status_reason) <= 256
    for forbidden in (
        "signed-token",
        "CERTIFICATE-CONTENT",
        "hunter2",
        "q" * 257,
    ):
        assert forbidden not in caught.value.job.status_reason
        assert forbidden not in str(caught.value)


def test_wait_job_timeout_copies_observation_before_sanitizing(
    tmp_path: Path, monkeypatch
) -> None:
    original_reason = "upstream rejected signed-token"
    observation = JobDetailResponse.from_dict(job_payload("running", original_reason))
    client = generated_client(
        tmp_path,
        lambda request, timeout: (_ for _ in ()).throw(
            AssertionError("network must not be reached")
        ),
    )
    monkeypatch.setattr(client, "job", lambda job_id: observation)

    with pytest.raises(control_client.ControlTimeout) as caught:
        client.wait_job(JOB_ID, timeout=0, interval=0)

    assert observation.status_reason == original_reason
    assert caught.value.job is not observation
    assert caught.value.job is not None
    assert caught.value.job.status_reason == "upstream rejected <redacted>"


def test_wait_job_honors_bounded_retry_after_on_get(tmp_path: Path) -> None:
    responses = iter(
        [
            Response(
                {"detail": "temporarily unavailable"},
                status=503,
                headers={"retry-after": "0"},
            ),
            Response(job_payload("succeeded")),
        ]
    )
    calls = []

    def opener(request, timeout):
        calls.append(request)
        return next(responses)

    client = generated_client(tmp_path, opener)
    started = time.monotonic()

    result = client.wait_job(JOB_ID, timeout=2, interval=0)

    elapsed = time.monotonic() - started
    assert result.state == "succeeded"
    assert 0.9 <= elapsed < 2
    assert [request.method for request in calls] == ["GET", "GET"]


def test_wait_job_retries_ambiguous_get_transport_failure(tmp_path: Path) -> None:
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise urllib.error.URLError("connection reset")
        return Response(job_payload("succeeded"))

    client = generated_client(tmp_path, opener)

    result = client.wait_job(JOB_ID, timeout=1, interval=0)

    assert result.state == "succeeded"
    assert [request.method for request in calls] == ["GET", "GET"]


def test_client_rejects_symlink_token(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.write_text("token")
    link = tmp_path / "token"
    link.symlink_to(actual)
    with pytest.raises(ControlClientError, match="non-symlink"):
        ControlClient("https://control.invalid", link)


def test_client_rejects_control_url_with_path(tmp_path: Path) -> None:
    with pytest.raises(ControlClientError, match="HTTPS origin"):
        ControlClient("https://control.invalid/admin", token_file(tmp_path))


def test_client_rejects_group_or_world_readable_token(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("signed-token")
    token.chmod(0o640)

    with pytest.raises(ControlClientError, match="permissions"):
        ControlClient("https://control.invalid", token)


def test_client_fails_closed_before_open_when_no_follow_flag_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    token = token_file(tmp_path)
    opened_paths: list[object] = []

    def tracking_open(path, flags):
        opened_paths.append(path)
        raise OSError("path open must not occur")

    monkeypatch.delattr(control_client.os, "O_NOFOLLOW")
    monkeypatch.setattr(control_client.os, "open", tracking_open)

    with pytest.raises(ControlClientError, match="cannot be opened safely"):
        ControlClient("https://control.invalid", token)

    assert opened_paths == []


def test_client_reads_token_from_single_validated_descriptor(tmp_path: Path) -> None:
    class ReplacingPath(type(tmp_path)):
        replacement: Path

        def read_text(self, *args, **kwargs):
            self.unlink()
            self.symlink_to(self.replacement)
            return super().read_text(*args, **kwargs)

    original = tmp_path / "token"
    original.write_text("original-token")
    original.chmod(0o600)
    replacement = tmp_path / "attacker-token"
    replacement.write_text("attacker-token")
    replacement.chmod(0o600)
    raced = ReplacingPath(original)
    raced.replacement = replacement
    calls = []

    def opener(request, timeout):
        calls.append(request)
        return Response({"digest": "abc"})

    client = ControlClient("https://control.invalid", raced, opener=opener)
    client.create_proposal({"base_commit": COMMIT, "changes": []})

    assert calls[0].headers["Authorization"] == "Bearer original-token"


def test_client_closes_token_descriptor_on_validation_error(
    tmp_path: Path, monkeypatch
) -> None:
    token = tmp_path / "token"
    token.write_bytes(b"\xff")
    token.chmod(0o600)
    real_open = control_client.os.open
    descriptors: list[int] = []

    def tracking_open(path, flags):
        descriptor = real_open(path, flags)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(control_client.os, "open", tracking_open)

    with pytest.raises(ControlClientError, match="token file is invalid"):
        ControlClient("https://control.invalid", token)

    assert len(descriptors) == 1
    with pytest.raises(OSError):
        control_client.os.fstat(descriptors[0])


def test_package_plan_accepts_durable_uuid_candidate_and_validation_identity() -> None:
    candidate_id = "00000000-0000-4000-8000-000000000010"
    validation_id = "00000000-0000-4000-8000-000000000011"
    plan = PackagePlanResponse.from_dict(
        {
            "candidate_id": candidate_id,
            "validation_id": validation_id,
            "digest": "sha256:" + "a" * 64,
            "state": "ready",
        }
    )
    assert plan.candidate_id == candidate_id
    assert plan.validation_id == validation_id

    client = object.__new__(ControlClient)
    assert client._package_candidate_id(candidate_id) == candidate_id


class FakeAdminClient:
    def __init__(self):
        self.payload = None

    def create_proposal(self, payload):
        self.payload = payload
        return {"digest": "same", "patch": "canonical"}


def test_vonkctl_admin_proposal_is_thin_api_adapter(tmp_path: Path, capsys) -> None:
    change = tmp_path / "change.json"
    change.write_text(
        json.dumps(
            {
                "base_commit": "a" * 40,
                "changes": [
                    {"path": "inventory/topology.json", "document": {"schema_version": 1}}
                ],
            }
        )
    )
    client = FakeAdminClient()
    assert (
        main(
            ["admin", "proposal", "--file", str(change), "--json"],
            control_client=client,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "digest": "same",
        "patch": "canonical",
    }
    assert client.payload["base_commit"] == "a" * 40
