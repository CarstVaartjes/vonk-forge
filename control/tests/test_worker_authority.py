from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from vonk_agent_protocol import canonical_message
from vonk_control.litellm import LiteLlmDeployment
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.route_runtime import (
    AcceptedEndpointEvidence,
    AtomicRouteBundlePublisher,
    PublishedRoute,
    RouteBundleRequest,
    endpoint_evidence_digest,
)
from vonk_control.worker_authority import (
    HttpWorkerAuthority,
    RepositoryAuthorityService,
    WorkerAuthorityError,
    install_worker_authority_routes,
    worker_document_signature,
)

COMMIT = "a" * 40
RECONCILIATION_ID = "00000000-0000-4000-8000-000000000001"
PLAN_DIGEST = "b" * 64
ROUTE = PublishedRoute(
    alias="chat",
    workload_id="model-a",
    api_base="http://10.0.0.10:8000/v1",
    requests_per_minute=10,
    tokens_per_minute=20,
)


def _client(*, eligible: bool = True) -> TestClient:
    service = RepositoryAuthorityService(
        current_commit=lambda: COMMIT,
        commit_eligible=lambda value: eligible and value == COMMIT,
        reconciliation_input=lambda reconciliation_id: (
            COMMIT,
            PLAN_DIGEST,
            (ROUTE,),
            "e" * 64,
        )
        if reconciliation_id == RECONCILIATION_ID
        else (_ for _ in ()).throw(ValueError("unknown reconciliation")),
        deployments=lambda commit, routes: (
            LiteLlmDeployment(
                model_name="hermes-agent",
                workload="model-a",
                api_base=routes[0].api_base,
                priority=1,
                requests_per_minute=10,
                tokens_per_minute=20,
            ),
        ),
        current_fleet_evidence=lambda: "e" * 64,
        clock=lambda: 100,
    )
    app = FastAPI()
    install_worker_authority_routes(app, service, token=b"w" * 32)
    return TestClient(app)


def test_internal_worker_authority_requires_exact_service_token() -> None:
    client = _client()
    body = {
        "schema_version": 1,
        "reconciliation_id": RECONCILIATION_ID,
        "commit": COMMIT,
        "plan_digest": PLAN_DIGEST,
        "nonce": "0" * 32,
        "routes": [
            {
                "alias": ROUTE.alias,
                "workload_id": ROUTE.workload_id,
                "api_base": ROUTE.api_base,
                "requests_per_minute": ROUTE.requests_per_minute,
                "tokens_per_minute": ROUTE.tokens_per_minute,
            }
        ],
    }

    assert client.post(
        "/internal/v1/repository/evaluate", json=body
    ).status_code == 401
    assert client.post(
        "/internal/v1/repository/evaluate",
        headers={
            "x-vonk-worker-signature": worker_document_signature(
                b"x" * 32,
                body,
                purpose="request",
            )
        },
        json=body,
    ).status_code == 401
    response = client.post(
        "/internal/v1/repository/evaluate",
        headers={
            "x-vonk-worker-signature": worker_document_signature(
                b"w" * 32,
                body,
                purpose="request",
            )
        },
        json=body,
    )

    assert response.status_code == 200
    assert response.json()["commit"] == COMMIT
    assert response.json()["nonce"] == "0" * 32
    assert response.json()["expires_at"] == 115


def test_internal_worker_authority_returns_commit_bound_hermes_deployments() -> None:
    client = _client()
    body = {
        "schema_version": 1,
        "reconciliation_id": RECONCILIATION_ID,
        "commit": COMMIT,
        "plan_digest": PLAN_DIGEST,
        "nonce": "1" * 32,
        "routes": [
            {
                "alias": ROUTE.alias,
                "workload_id": ROUTE.workload_id,
                "api_base": ROUTE.api_base,
                "requests_per_minute": ROUTE.requests_per_minute,
                "tokens_per_minute": ROUTE.tokens_per_minute,
            }
        ],
    }
    response = client.post(
        "/internal/v1/repository/evaluate",
        headers={
            "x-vonk-worker-signature": worker_document_signature(
                b"w" * 32,
                body,
                purpose="request",
            )
        },
        json=body,
    )

    assert response.status_code == 200
    assert response.json()["commit"] == COMMIT
    assert response.json()["nonce"] == "1" * 32
    assert response.json()["current"] is True
    assert response.json()["eligible"] is True
    assert response.json()["deployments"] == [
        {
            "model_name": "hermes-agent",
            "workload": "model-a",
            "api_base": ROUTE.api_base,
            "priority": 1,
            "requests_per_minute": 10,
            "tokens_per_minute": 20,
        }
    ]


def test_internal_worker_authority_fails_closed_before_repository_policy_output() -> None:
    client = _client(eligible=False)
    body = {
        "schema_version": 1,
        "reconciliation_id": RECONCILIATION_ID,
        "commit": COMMIT,
        "plan_digest": PLAN_DIGEST,
        "nonce": "2" * 32,
        "routes": [
            {
                "alias": ROUTE.alias,
                "workload_id": ROUTE.workload_id,
                "api_base": ROUTE.api_base,
                "requests_per_minute": ROUTE.requests_per_minute,
                "tokens_per_minute": ROUTE.tokens_per_minute,
            }
        ],
    }
    response = client.post(
        "/internal/v1/repository/evaluate",
        headers={
            "x-vonk-worker-signature": worker_document_signature(
                b"w" * 32,
                body,
                purpose="request",
            )
        },
        json=body,
    )

    assert response.status_code == 200
    assert response.json()["eligible"] is False
    assert response.json()["deployments"] == []


def test_worker_prefetches_once_then_consumes_only_exact_cached_authority() -> None:
    calls: list[str] = []

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

    def open_request(request, *, timeout):
        assert timeout == 3
        body = json.loads(request.data)
        calls.append(request.full_url)
        response = {
            "schema_version": 1,
            "reconciliation_id": body["reconciliation_id"],
            "commit": COMMIT,
            "plan_digest": body["plan_digest"],
            "nonce": body["nonce"],
            "current": True,
            "eligible": True,
            "fleet_evidence_current": True,
            "routes_sha256": hashlib.sha256(b"[]").hexdigest(),
            "deployments": [],
            "issued_at": 100,
            "expires_at": 115,
        }
        response["signature"] = worker_document_signature(
            b"w" * 32,
            response,
            purpose="response",
        )
        return Response(json.dumps(response).encode())

    authority = HttpWorkerAuthority(
        "http://control-api:8000",
        b"w" * 32,
        opener=open_request,
        clock=lambda: 100,
    )

    authority.prefetch(RECONCILIATION_ID, COMMIT, PLAN_DIGEST, ())
    assert authority.authorized(RECONCILIATION_ID, COMMIT, PLAN_DIGEST, ()) is True
    assert authority.eligible(COMMIT) is True
    assert authority.current_commit() == COMMIT
    assert calls == ["http://control-api:8000/internal/v1/repository/evaluate"]

    with pytest.raises(WorkerAuthorityError):
        authority.authorized(
            "00000000-0000-4000-8000-000000000002",
            COMMIT,
            PLAN_DIGEST,
            (),
        )
    with pytest.raises(WorkerAuthorityError):
        authority.authorized(RECONCILIATION_ID, COMMIT, "c" * 64, ())


def test_worker_publication_uses_prefetched_route_policy_without_network() -> None:
    calls = 0

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

    def open_request(request, *, timeout):
        nonlocal calls
        calls += 1
        body = json.loads(request.data)
        response = {
            "schema_version": 1,
            "reconciliation_id": body["reconciliation_id"],
            "commit": body["commit"],
            "plan_digest": body["plan_digest"],
            "nonce": body["nonce"],
            "current": True,
            "eligible": True,
            "fleet_evidence_current": True,
            "routes_sha256": hashlib.sha256(
                json.dumps(
                    body["routes"], sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
            "deployments": [
                {
                    "model_name": "hermes-agent",
                    "workload": "model-a",
                    "api_base": ROUTE.api_base,
                    "priority": 1,
                    "requests_per_minute": 10,
                    "tokens_per_minute": 20,
                }
            ],
            "issued_at": 100,
            "expires_at": 115,
        }
        response["signature"] = worker_document_signature(
            b"w" * 32, response, purpose="response"
        )
        return Response(json.dumps(response).encode())

    authority = HttpWorkerAuthority(
        "http://control-api:8000",
        b"w" * 32,
        opener=open_request,
        clock=lambda: 100,
    )
    authority.prefetch(RECONCILIATION_ID, COMMIT, PLAN_DIGEST, (ROUTE,))

    assert authority.deployments(COMMIT, (ROUTE,))[0].model_name == "hermes-agent"
    assert calls == 1


def test_atomic_publisher_never_performs_authority_network_io_under_file_lock(
    tmp_path: Path,
) -> None:
    calls = 0

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

    def open_request(request, *, timeout):
        nonlocal calls
        calls += 1
        body = json.loads(request.data)
        response = {
            "schema_version": 1,
            "reconciliation_id": body["reconciliation_id"],
            "commit": body["commit"],
            "plan_digest": body["plan_digest"],
            "nonce": body["nonce"],
            "current": True,
            "eligible": True,
            "fleet_evidence_current": True,
            "routes_sha256": hashlib.sha256(
                json.dumps(
                    body["routes"], sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
            "deployments": [
                {
                    "model_name": "hermes-agent",
                    "workload": ROUTE.workload_id,
                    "api_base": ROUTE.api_base,
                    "priority": 1,
                    "requests_per_minute": ROUTE.requests_per_minute,
                    "tokens_per_minute": ROUTE.tokens_per_minute,
                }
            ],
            "issued_at": 100,
            "expires_at": 115,
        }
        response["signature"] = worker_document_signature(
            b"w" * 32, response, purpose="response"
        )
        return Response(json.dumps(response).encode())

    authority = HttpWorkerAuthority(
        "http://control-api:8000",
        b"w" * 32,
        opener=open_request,
        clock=lambda: 100,
    )
    authority.prefetch(RECONCILIATION_ID, COMMIT, PLAN_DIGEST, (ROUTE,))
    now = datetime(2026, 8, 5, tzinfo=UTC)
    quota = {
        "requests_per_minute": ROUTE.requests_per_minute,
        "tokens_per_minute": ROUTE.tokens_per_minute,
    }
    route_document = {
        "workload_id": ROUTE.workload_id,
        "nodes": ["spk_" + "1" * 32],
        "entrypoint_node_id": "spk_" + "1" * 32,
        "scheme": "http",
        "port": 8000,
        "path": "/v1",
        "quota": quota,
        "quota_digest": hashlib.sha256(canonical_message(quota)).hexdigest(),
    }
    operation_id = f"model-a:{'spk_' + '1' * 32}:workload.verify"
    verify_digest = "d" * 64
    evidence_digest = endpoint_evidence_digest(
        node_id="spk_" + "1" * 32,
        address="10.0.0.10",
        observed_at=now,
        operation_id=operation_id,
        verify_evidence_digest=verify_digest,
    )
    publisher = AtomicRouteBundlePublisher(
        tmp_path / "routes",
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: now,
        litellm_deployments=authority.deployments,
    )
    request = RouteBundleRequest(
        reconciliation_id=RECONCILIATION_ID,
        plan_digest=PLAN_DIGEST,
        evidence_set_digest="e" * 64,
        routes={"chat": route_document},
        endpoints={
            "spk_" + "1" * 32: AcceptedEndpointEvidence(
                node_id="spk_" + "1" * 32,
                address="10.0.0.10",
                observed_at=now,
                operation_id=operation_id,
                verify_evidence_digest=verify_digest,
                evidence_digest=evidence_digest,
            )
        },
        expires_at=now + timedelta(seconds=60),
        base_commit=COMMIT,
    )

    publisher.publish(request)
    publisher.publish(request)

    assert calls == 1
    with pytest.raises(WorkerAuthorityError):
        authority.deployments(
            COMMIT,
            (
                PublishedRoute(
                    alias=ROUTE.alias,
                    workload_id=ROUTE.workload_id,
                    api_base="http://10.0.0.11:8000/v1",
                    requests_per_minute=ROUTE.requests_per_minute,
                    tokens_per_minute=ROUTE.tokens_per_minute,
                ),
            ),
        )
    assert calls == 1


def test_repository_head_change_during_policy_evaluation_fails_closed() -> None:
    heads = iter((COMMIT, "b" * 40))
    policy_calls: list[str] = []
    service = RepositoryAuthorityService(
        current_commit=lambda: next(heads),
        commit_eligible=lambda _commit: True,
        reconciliation_input=lambda _reconciliation_id: (
            COMMIT,
            PLAN_DIGEST,
            (ROUTE,),
            "e" * 64,
        ),
        current_fleet_evidence=lambda: "e" * 64,
        deployments=lambda commit, _routes: (
            policy_calls.append(commit) or ()
        ),
        clock=lambda: 100,
    )

    result = service.evaluate(
        RECONCILIATION_ID,
        COMMIT,
        PLAN_DIGEST,
        (ROUTE,),
    )

    assert result["current"] is False
    assert result["eligible"] is False
    assert result["deployments"] == []
    assert policy_calls == [COMMIT]


def test_fleet_evidence_change_during_authority_evaluation_fails_closed() -> None:
    fleet = iter(("e" * 64, "f" * 64))
    service = RepositoryAuthorityService(
        current_commit=lambda: COMMIT,
        commit_eligible=lambda _commit: True,
        reconciliation_input=lambda _reconciliation_id: (
            COMMIT,
            PLAN_DIGEST,
            (ROUTE,),
            "e" * 64,
        ),
        current_fleet_evidence=lambda: next(fleet),
        deployments=lambda _commit, _routes: (),
        clock=lambda: 100,
    )

    result = service.evaluate(RECONCILIATION_ID, COMMIT, PLAN_DIGEST, (ROUTE,))

    assert result["fleet_evidence_current"] is False
    assert result["deployments"] == []


def test_worker_reports_explicit_signed_fleet_evidence_authority_loss() -> None:
    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

    def open_request(request, *, timeout):
        body = json.loads(request.data)
        response = {
            "schema_version": 1,
            "reconciliation_id": body["reconciliation_id"],
            "commit": body["commit"],
            "plan_digest": body["plan_digest"],
            "nonce": body["nonce"],
            "current": True,
            "eligible": True,
            "fleet_evidence_current": False,
            "routes_sha256": hashlib.sha256(b"[]").hexdigest(),
            "deployments": [],
            "issued_at": 100,
            "expires_at": 115,
        }
        response["signature"] = worker_document_signature(
            b"w" * 32, response, purpose="response"
        )
        return Response(json.dumps(response).encode())

    authority = HttpWorkerAuthority(
        "http://control-api:8000",
        b"w" * 32,
        opener=open_request,
        clock=lambda: 100,
    )
    authority.prefetch(RECONCILIATION_ID, COMMIT, PLAN_DIGEST, ())

    assert authority.authorization_reason(
        RECONCILIATION_ID, COMMIT, PLAN_DIGEST, ()
    ) == "fleet acceptance evidence changed since planning"


@pytest.mark.parametrize("mismatch", ("reconciliation", "commit", "plan", "route"))
def test_internal_worker_authority_rejects_scope_not_in_persisted_plan(
    mismatch: str,
) -> None:
    client = _client()
    route = {
        "alias": ROUTE.alias,
        "workload_id": ROUTE.workload_id,
        "api_base": ROUTE.api_base,
        "requests_per_minute": ROUTE.requests_per_minute,
        "tokens_per_minute": ROUTE.tokens_per_minute,
    }
    body = {
        "schema_version": 1,
        "reconciliation_id": RECONCILIATION_ID,
        "commit": COMMIT,
        "plan_digest": PLAN_DIGEST,
        "nonce": "3" * 32,
        "routes": [route],
    }
    if mismatch == "reconciliation":
        body["reconciliation_id"] = "00000000-0000-4000-8000-000000000002"
    elif mismatch == "commit":
        body["commit"] = "c" * 40
    elif mismatch == "plan":
        body["plan_digest"] = "c" * 64
    else:
        route["api_base"] = "http://10.0.0.11:8000/v1"
    response = client.post(
        "/internal/v1/repository/evaluate",
        headers={
            "x-vonk-worker-signature": worker_document_signature(
                b"w" * 32,
                body,
                purpose="request",
            )
        },
        json=body,
    )

    assert response.status_code == 503


@pytest.mark.parametrize(
    ("current", "eligible", "deployments"),
    (
        (False, True, []),
        (True, False, [{"untrusted": "deployment"}]),
    ),
)
def test_worker_rejects_internally_inconsistent_signed_decision(
    current: bool,
    eligible: bool,
    deployments: list[object],
) -> None:
    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

    def open_request(request, *, timeout):
        body = json.loads(request.data)
        response = {
            "schema_version": 1,
            "reconciliation_id": body["reconciliation_id"],
            "commit": body["commit"],
            "plan_digest": body["plan_digest"],
            "nonce": body["nonce"],
            "current": current,
            "eligible": eligible,
            "fleet_evidence_current": True,
            "routes_sha256": hashlib.sha256(b"[]").hexdigest(),
            "deployments": deployments,
            "issued_at": 100,
            "expires_at": 115,
        }
        response["signature"] = worker_document_signature(
            b"w" * 32, response, purpose="response"
        )
        return Response(json.dumps(response).encode())

    authority = HttpWorkerAuthority(
        "http://control-api:8000", b"w" * 32, opener=open_request, clock=lambda: 100
    )
    with pytest.raises(WorkerAuthorityError):
        authority.prefetch(RECONCILIATION_ID, COMMIT, PLAN_DIGEST, ())


def test_failed_second_prefetch_cannot_reuse_first_positive_cache() -> None:
    calls = 0

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

    def open_request(request, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("authority timeout")
        body = json.loads(request.data)
        response = {
            "schema_version": 1,
            "reconciliation_id": body["reconciliation_id"],
            "commit": body["commit"],
            "plan_digest": body["plan_digest"],
            "nonce": body["nonce"],
            "current": True,
            "eligible": True,
            "fleet_evidence_current": True,
            "routes_sha256": hashlib.sha256(b"[]").hexdigest(),
            "deployments": [],
            "issued_at": 100,
            "expires_at": 115,
        }
        response["signature"] = worker_document_signature(
            b"w" * 32, response, purpose="response"
        )
        return Response(json.dumps(response).encode())

    now = {"value": 100}
    authority = HttpWorkerAuthority(
        "http://control-api:8000",
        b"w" * 32,
        opener=open_request,
        clock=lambda: now["value"],
    )
    authority.prefetch(RECONCILIATION_ID, COMMIT, PLAN_DIGEST, ())
    with pytest.raises(WorkerAuthorityError):
        authority.prefetch(RECONCILIATION_ID, COMMIT, PLAN_DIGEST, ())
    with pytest.raises(WorkerAuthorityError):
        authority.authorized(RECONCILIATION_ID, COMMIT, PLAN_DIGEST, ())

    calls = 0
    authority.prefetch(RECONCILIATION_ID, COMMIT, PLAN_DIGEST, ())
    now["value"] = 115
    with pytest.raises(WorkerAuthorityError):
        authority.authorized(RECONCILIATION_ID, COMMIT, PLAN_DIGEST, ())


@pytest.mark.parametrize("fault", ("signature", "nonce", "expired", "redirect", "oversized"))
def test_worker_rejects_tampered_stale_redirected_or_oversized_authority(
    fault: str,
) -> None:
    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

        def geturl(self) -> str:
            if fault == "redirect":
                return "http://attacker.invalid/authority"
            return "http://control-api:8000/internal/v1/repository/evaluate"

    def open_request(request, *, timeout):
        assert timeout == 3
        if fault == "oversized":
            return Response(b"x" * 65_537)
        body = json.loads(request.data)
        response = {
            "schema_version": 1,
            "reconciliation_id": body["reconciliation_id"],
            "commit": COMMIT,
            "plan_digest": body["plan_digest"],
            "nonce": "f" * 32 if fault == "nonce" else body["nonce"],
            "current": True,
            "eligible": True,
            "fleet_evidence_current": True,
            "routes_sha256": hashlib.sha256(b"[]").hexdigest(),
            "deployments": [],
            "issued_at": 80 if fault == "expired" else 100,
            "expires_at": 95 if fault == "expired" else 115,
        }
        response["signature"] = worker_document_signature(
            b"w" * 32,
            response,
            purpose="response",
        )
        if fault == "signature":
            response["signature"] = "0" * 64
        return Response(json.dumps(response).encode())

    authority = HttpWorkerAuthority(
        "http://control-api:8000",
        b"w" * 32,
        opener=open_request,
        clock=lambda: 100,
    )

    with pytest.raises(WorkerAuthorityError):
        authority.prefetch(RECONCILIATION_ID, COMMIT, PLAN_DIGEST, ())


def test_worker_http_client_disables_environment_proxies() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src/vonk_control/worker_authority.py"
    ).read_text()

    assert "ProxyHandler({})" in source


def test_external_caddy_listener_denies_internal_worker_routes_before_fallback() -> None:
    root = Path(__file__).resolve().parents[2]
    caddy = (root / "deploy/compose/Caddyfile").read_text()
    tailnet = caddy.split(":8080 {", 1)[1].split(
        "# Bootstrap is server-authenticated", 1
    )[0]

    assert "path /internal/*" in tailnet
    assert tailnet.index("path /internal/*") < tailnet.index(
        "import browser_control_proxy"
    )
