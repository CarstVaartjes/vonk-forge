from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_agent_protocol import LifecycleState
from vonk_control.bounded_retry import REQUEST_PAUSES
from vonk_control.model_cache import (
    ModelCacheService,
    ModelCacheStorageError,
)
from vonk_control.models import Base, CatalogDocument, CatalogDocumentRevision
from vonk_forge_contracts import ModelDefinition, document_sha256

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
REPOSITORY = "https://github.com/valeoai/NAF"
RELEASE_ID = 264676230
ASSET_ID = 320107386
OWNER = "valeoai"
REPO = "NAF"
ASSET_NAME = "weights.safetensors"
RELEASE_URL = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/{RELEASE_ID}"
ASSET_URL = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/assets/{ASSET_ID}"
CDN_URL = (
    "https://release-assets.githubusercontent.com/download/asset?sig=fixture-secret"
)
GITHUB_USER_AGENT = "vonk-forge/0.1.1"


@pytest.fixture
def sessions(tmp_path: Path):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    yield maker
    engine.dispose()


def _model(data: bytes) -> ModelDefinition:
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    document["identity"]["publisher"] = "vonk-forge"
    document["identity"]["slug"] = "github-release-cache"
    document["identity"]["model"]["publisher"] = "vonk-forge"
    document["identity"]["model"]["slug"] = "github-release-cache"
    document["requires_token"] = False
    document["source"] = {
        "provider": "github-release",
        "repository": REPOSITORY,
        "release_id": RELEASE_ID,
        "assets": [{"file_id": "weights", "asset_id": ASSET_ID}],
    }
    document["files"] = [
        {
            "id": "weights",
            "path": ASSET_NAME,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "roles": ["weights"],
        }
    ]
    return ModelDefinition.model_validate(document)


def _insert_model(sessions, model: ModelDefinition) -> tuple[str, str]:
    digest = document_sha256(model.model_dump(mode="json"))
    with sessions.begin() as session:
        document = CatalogDocument(
            kind="model",
            publisher=model.identity.publisher,
            slug=model.identity.slug,
            title="GitHub release cache test",
            created_by="test",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(document)
        session.flush()
        session.add(
            CatalogDocumentRevision(
                document_id=document.id,
                kind="model",
                publisher=model.identity.publisher,
                slug=model.identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=model.model_dump(mode="json"),
                content_digest=digest,
                projected={},
                created_by="test",
                created_at=NOW,
            )
        )
    return digest, f"{model.identity.publisher}/{model.identity.slug}"


def _release_document(data: bytes) -> dict[str, object]:
    return {
        "id": RELEASE_ID,
        "tag_name": "mutable-tag-is-not-the-pin",
        "assets": [
            {
                "id": ASSET_ID,
                "name": ASSET_NAME,
                "size": len(data),
                "state": "uploaded",
                "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
                "browser_download_url": "https://github.com/valeoai/NAF/releases/download/tag/weights.safetensors",
                "created_at": "2026-09-01T12:00:00Z",
            }
        ],
    }


def _assert_anonymous(request: httpx2.Request) -> None:
    assert request.headers.get("authorization") is None
    assert request.headers.get("cookie") is None
    assert request.headers.get("user-agent") == GITHUB_USER_AGENT


def _client(handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.Client:
    return httpx2.Client(
        transport=httpx2.MockTransport(handler),
        follow_redirects=False,
        headers={
            "Authorization": "Bearer client-sentinel",
            "Cookie": "session=sentinel",
            "User-Agent": "private-client-sentinel",
        },
        cookies={"auth-cookie": "sentinel"},
        auth=("client-user", "client-password"),
    )


def _service(sessions, root: Path, client: httpx2.Client) -> ModelCacheService:
    return ModelCacheService(
        sessions,
        root,
        reserve_bytes=0,
        http_client=client,
        fixture_sources=False,
        clock=lambda: NOW,
    )


def _serve_release_and_asset(
    data: bytes,
    requests: list[httpx2.Request],
    *,
    release: dict[str, object] | None = None,
    cdn_handler: Callable[[httpx2.Request], httpx2.Response] | None = None,
) -> Callable[[httpx2.Request], httpx2.Response]:
    release_document = release or _release_document(data)

    def handler(request: httpx2.Request) -> httpx2.Response:
        _assert_anonymous(request)
        requests.append(request)
        if str(request.url) == RELEASE_URL:
            return httpx2.Response(200, request=request, json=release_document)
        if str(request.url) == ASSET_URL:
            return httpx2.Response(
                302,
                request=request,
                headers={"Location": CDN_URL},
            )
        if request.url.host == "release-assets.githubusercontent.com":
            if cdn_handler is not None:
                return cdn_handler(request)
            return httpx2.Response(200, request=request, content=data)
        raise AssertionError(f"unexpected provider request: {request.url}")

    return handler


def _preview_and_start(
    service: ModelCacheService, digest: str, request_key: str, **kwargs
):
    preview = service.download_preview(model_content_sha256=digest)
    assert preview["blockers"] == []
    operation = service.start_download(
        actor="operator",
        request_key=request_key,
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=digest,
        **kwargs,
    )
    return preview, operation


def test_github_release_download_is_asset_id_bound_anonymous_and_reusable_offline(
    sessions, tmp_path: Path
) -> None:
    data = b"verified NAF release asset bytes"
    digest, _selector = _insert_model(sessions, _model(data))
    requests: list[httpx2.Request] = []
    client = _client(_serve_release_and_asset(data, requests))
    service = _service(sessions, tmp_path / "nas-cache", client)
    try:
        manifest = service.resolve_artifact_set(model_content_sha256=digest)
        artifact = manifest.artifacts[0]
        assert artifact.kind == "github-release.asset"
        assert artifact.repository == REPOSITORY
        assert artifact.revision == f"github-release:{RELEASE_ID}"
        assert artifact.source == ASSET_URL

        _preview, accepted = _preview_and_start(
            service,
            digest,
            "00000000-0000-4000-8000-000000001201",
        )
        assert service.run_pending() == 1
        finished = service.get_operation(accepted.id)
        assert finished.state == "succeeded"
        assert service._object_path(artifact.sha256).read_bytes() == data
        assert [str(request.url) for request in requests] == [
            ASSET_URL,
            CDN_URL,
        ]
        assert all(request.headers.get("range") is None for request in requests)
    finally:
        service.close()
        client.close()

    offline_calls: list[httpx2.Request] = []

    def forbidden(request: httpx2.Request) -> httpx2.Response:
        offline_calls.append(request)
        raise AssertionError("verified local bytes must not require GitHub")

    offline_client = _client(forbidden)
    offline = _service(sessions, tmp_path / "nas-cache", offline_client)
    try:
        preview = offline.download_preview(model_content_sha256=digest)
        assert preview["new_bytes"] == 0
        assert preview["already_cached_bytes"] == len(data)
        replay, operation = _preview_and_start(
            offline,
            digest,
            "00000000-0000-4000-8000-000000001202",
        )
        assert replay["new_bytes"] == 0
        offline.run_pending()
        assert offline.get_operation(operation.id).state == "succeeded"
        assert offline_calls == []
    finally:
        offline.close()
        offline_client.close()


def test_github_source_accepts_large_ids_permitted_by_canonical_contract(
    sessions, tmp_path: Path
) -> None:
    data = b"canonical integer IDs are not capped by the cache layer"
    document = _model(data).model_dump(mode="json")
    release_id = 123456789012345678901
    asset_id = 987654321098765432109
    source = document["source"]
    assert isinstance(source, dict)
    source["release_id"] = release_id
    assets = source["assets"]
    assert isinstance(assets, list) and isinstance(assets[0], dict)
    assets[0]["asset_id"] = asset_id
    model = ModelDefinition.model_validate(document)
    digest, _selector = _insert_model(sessions, model)

    def forbidden(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(
            "resolving an exact asset identity must not use the network"
        )

    client = _client(forbidden)
    service = _service(sessions, tmp_path / "large-ids-cache", client)
    try:
        spec = service.resolve_artifact_set(model_content_sha256=digest).artifacts[0]
        assert spec.revision == f"github-release:{release_id}"
        assert spec.source == (
            f"https://api.github.com/repos/{OWNER}/{REPO}/releases/assets/{asset_id}"
        )
        service._validate_http_download(spec)
    finally:
        service.close()
        client.close()


@pytest.mark.parametrize(
    "change",
    [
        "wrong-release",
        "missing-asset",
        "duplicate-asset",
        "wrong-name",
        "wrong-size",
        "wrong-digest",
        "malformed-size",
    ],
)
def test_mutable_release_metadata_cannot_veto_exact_verified_content(
    sessions, tmp_path: Path, change: str
) -> None:
    data = b"metadata must bind before binary bytes"
    digest, _selector = _insert_model(sessions, _model(data))
    release = _release_document(data)
    assets = release["assets"]
    assert isinstance(assets, list) and isinstance(assets[0], dict)
    asset = assets[0]
    if change == "wrong-release":
        release["id"] = RELEASE_ID + 1
    elif change == "missing-asset":
        release["assets"] = []
    elif change == "duplicate-asset":
        release["assets"] = [asset, dict(asset)]
    elif change == "wrong-name":
        asset["name"] = "another.safetensors"
    elif change == "wrong-size":
        asset["size"] = len(data) + 1
    elif change == "wrong-digest":
        asset["digest"] = "sha256:" + ("0" * 64)
    elif change == "malformed-size":
        asset["size"] = str(len(data))
    requests: list[httpx2.Request] = []
    client = _client(_serve_release_and_asset(data, requests, release=release))
    service = _service(sessions, tmp_path / change, client)
    try:
        _, operation = _preview_and_start(service, digest, str(uuid4()))
        service.run_pending()
        spec = service.resolve_artifact_set(model_content_sha256=digest).artifacts[0]
        assert service.get_operation(operation.id).state == LifecycleState.SUCCEEDED
        assert service._object_path(spec.sha256).read_bytes() == data
        assert [str(request.url) for request in requests] == [ASSET_URL, CDN_URL]
    finally:
        service.close()
        client.close()


@pytest.mark.parametrize(
    ("payload_shape", "expected_failure_code"),
    [
        ("same-size-wrong-digest", "integrity_mismatch"),
        ("shorter-than-pin", "model_cache.source_truncated"),
        ("longer-than-pin", "integrity_mismatch"),
    ],
)
def test_github_download_rejects_wrong_binary_bytes(
    sessions,
    tmp_path: Path,
    payload_shape: str,
    expected_failure_code: str,
) -> None:
    expected_bytes = b"expected model content pinned by sha256 and size"
    actual_size = {
        "same-size-wrong-digest": len(expected_bytes),
        "shorter-than-pin": len(expected_bytes) - 1,
        "longer-than-pin": len(expected_bytes) + 1,
    }[payload_shape]
    actual_bytes = b"x" * actual_size
    assert actual_bytes != expected_bytes
    digest, _selector = _insert_model(sessions, _model(expected_bytes))
    requests: list[httpx2.Request] = []

    def cdn(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, request=request, content=actual_bytes)

    client = _client(
        _serve_release_and_asset(expected_bytes, requests, cdn_handler=cdn)
    )
    service = _service(sessions, tmp_path / "bad-binary-cache", client)
    try:
        _preview, accepted = _preview_and_start(
            service,
            digest,
            "00000000-0000-4000-8000-000000001204",
        )
        service.run_pending()
        finished = service.get_operation(accepted.id)
        assert finished.state in {"queued", "failed"}
        assert finished.failure is not None
        assert finished.failure["code"] == expected_failure_code
        object_digest = hashlib.sha256(expected_bytes).hexdigest()
        assert not service._object_path(object_digest).exists()
        assert any(str(request.url) == CDN_URL for request in requests)
    finally:
        service.close()
        client.close()


def test_github_asset_redirect_rejects_untrusted_hosts_without_credentials(
    sessions, tmp_path: Path
) -> None:
    data = b"redirect must remain within the release CDN"
    digest, _selector = _insert_model(sessions, _model(data))
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        _assert_anonymous(request)
        requests.append(request)
        return httpx2.Response(
            302,
            request=request,
            headers={"Location": "https://evil.example/steal?sig=private"},
        )

    client = _client(handler)
    service = _service(sessions, tmp_path / "redirect-cache", client)
    try:
        spec = service.resolve_artifact_set(model_content_sha256=digest).artifacts[0]
        with pytest.raises(ModelCacheStorageError) as failure:
            service._open_github_release_asset(client, spec, {})
        assert failure.value.code == "model_cache.redirect_forbidden"
        assert [str(request.url) for request in requests] == [ASSET_URL]
    finally:
        service.close()
        client.close()


def test_github_transfer_error_does_not_persist_signed_cdn_url(
    sessions, tmp_path: Path
) -> None:
    data = b"expected content before the CDN connection fails"
    digest, _selector = _insert_model(sessions, _model(data))
    requests: list[httpx2.Request] = []

    def cdn(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout(
            f"timeout while reading {request.url}", request=request
        )

    client = _client(_serve_release_and_asset(data, requests, cdn_handler=cdn))
    service = _service(sessions, tmp_path / "cdn-timeout-cache", client)
    try:
        _preview, accepted = _preview_and_start(
            service,
            digest,
            "00000000-0000-4000-8000-000000001205",
        )
        service.run_pending()
        failed = service.get_operation(accepted.id)
        assert failed.failure is not None
        durable_view = json.dumps(
            {
                "failure": failed.failure,
                "last_error": failed.last_error,
                "progress": failed.progress,
            },
            sort_keys=True,
        )
        assert "fixture-secret" not in durable_view
        assert CDN_URL not in durable_view
        assert [str(request.url) for request in requests] == [ASSET_URL, CDN_URL] * (
            len(REQUEST_PAUSES) + 1
        )
        assert all(
            request.headers.get("authorization") is None
            and request.headers.get("cookie") is None
            for request in requests
        )
    finally:
        service.close()
        client.close()


class _ByteStream(httpx2.SyncByteStream):
    def __init__(self, data: bytes, fragment: int = 256 * 1024) -> None:
        self.data = data
        self.fragment = fragment

    def __iter__(self):
        for offset in range(0, len(self.data), self.fragment):
            yield self.data[offset : offset + self.fragment]


def test_github_release_download_resumes_partial_bytes_after_service_restart(
    sessions, tmp_path: Path
) -> None:
    data = bytes(range(256)) * 12_000
    digest, _selector = _insert_model(sessions, _model(data))
    ranges: list[str | None] = []
    requests: list[httpx2.Request] = []

    def cdn(request: httpx2.Request) -> httpx2.Response:
        range_header = request.headers.get("range")
        ranges.append(range_header)
        if range_header is None:
            return httpx2.Response(
                200,
                request=request,
                stream=_ByteStream(data),
                headers={"Content-Encoding": "identity"},
            )
        match = re.fullmatch(r"bytes=(\d+)-", range_header)
        assert match is not None
        start = int(match.group(1))
        return httpx2.Response(
            206,
            request=request,
            stream=_ByteStream(data[start:]),
            headers={
                "Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}",
                "Content-Encoding": "identity",
            },
        )

    client = _client(_serve_release_and_asset(data, requests, cdn_handler=cdn))
    service = _service(sessions, tmp_path / "resume-cache", client)
    preview = service.download_preview(model_content_sha256=digest)
    interrupted = service.start_download(
        actor="operator",
        request_key="00000000-0000-4000-8000-000000001203",
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=digest,
        interrupt_after_bytes=1_100_000,
    )
    assert interrupted.state == LifecycleState.BACKOFF
    object_digest = hashlib.sha256(data).hexdigest()
    partial = (
        service.root
        / "partials"
        / str(interrupted.artifact_set_sha256)
        / f"{object_digest}.part"
    )
    assert 0 < partial.stat().st_size < len(data)
    offset = partial.stat().st_size
    service.close()
    client.close()

    resumed_requests: list[httpx2.Request] = []
    resumed_client = _client(
        _serve_release_and_asset(data, resumed_requests, cdn_handler=cdn)
    )
    restarted = _service(sessions, tmp_path / "resume-cache", resumed_client)
    try:
        assert restarted.resume_operations() == 1
        restarted.run_pending()
        finished = restarted.get_operation(interrupted.id)
        assert finished.state == "succeeded"
        assert not partial.exists()
        assert restarted._object_path(object_digest).read_bytes() == data
        assert ranges[0] is None
        assert ranges[-1] == f"bytes={offset}-"
        assert all(
            request.headers.get("authorization") is None
            and request.headers.get("cookie") is None
            for request in requests + resumed_requests
        )
        assert any(str(request.url) == ASSET_URL for request in resumed_requests)
        assert any(str(request.url) == CDN_URL for request in resumed_requests)
    finally:
        restarted.close()
        resumed_client.close()


@pytest.mark.parametrize("status", [401, 403])
def test_denied_source_never_transfers_and_new_authorized_request_progresses(
    sessions, tmp_path, status
):
    data = b"public access must be explicit"
    digest, _selector = _insert_model(sessions, _model(data))
    requests = []
    healthy = _serve_release_and_asset(data, requests)
    denied = True

    def handler(request):
        if denied:
            requests.append(request)
            return httpx2.Response(status, request=request)
        return healthy(request)

    client = _client(handler)
    service = _service(sessions, tmp_path / "source-access", client)
    try:
        _, _operation = _preview_and_start(
            service, digest, "00000000-0000-4000-8000-000000000081"
        )
        service.run_pending()
        assert service.get_operation(_operation.id).state == LifecycleState.FAILED
        assert not service._object_path(hashlib.sha256(data).hexdigest()).exists()
        assert all(str(request.url) == ASSET_URL for request in requests)
        denied = False
        _, fresh = _preview_and_start(
            service, digest, "00000000-0000-4000-8000-000000000082"
        )
        service.run_pending()
        assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED
        assert (
            service._object_path(hashlib.sha256(data).hexdigest()).read_bytes() == data
        )
    finally:
        service.close()
        client.close()


@pytest.mark.parametrize(
    "status,headers",
    [
        (429, {"Retry-After": "17"}),
        (304, {"Retry-After": "17"}),
        (403, {"X-RateLimit-Remaining": "0", "Retry-After": "17"}),
    ],
)
def test_provider_backoff_is_durable_and_current_request_recovers(
    sessions, tmp_path, status, headers
):
    data = b"verified after backoff"
    digest, _selector = _insert_model(sessions, _model(data))
    requests = []
    healthy = _serve_release_and_asset(data, requests)
    limited = True
    now = NOW

    def handler(request):
        if limited:
            requests.append(request)
            return httpx2.Response(status, request=request, headers=headers)
        return healthy(request)

    client = _client(handler)
    service = _service(sessions, tmp_path / "backoff", client)
    service._clock = lambda: now
    try:
        _, operation = _preview_and_start(
            service, digest, "00000000-0000-4000-8000-000000000071"
        )
        service.run_pending()
        assert not service._object_path(hashlib.sha256(data).hexdigest()).exists()
        count = len(requests)
        limited = False
        now += timedelta(seconds=16)
        assert service._claim_operations(limit=1, respect_backoff=True) == []
        assert len(requests) == count
        now += timedelta(minutes=2)
        claimed = service._claim_operations(limit=1, respect_backoff=True)
        assert len(claimed) == 1
        assert claimed[0][0] == operation.id
        service._run_download(operation.id, force=False)
        assert service.get_operation(operation.id).state == LifecycleState.SUCCEEDED
        assert (
            service._object_path(hashlib.sha256(data).hexdigest()).read_bytes() == data
        )
        _, fresh = _preview_and_start(
            service, digest, "00000000-0000-4000-8000-000000000072"
        )
        service.run_pending()
        assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED
    finally:
        service.close()
        client.close()


@pytest.mark.parametrize("response_shape", ["missing-location", "cdn-redirect"])
def test_incomplete_redirect_observes_until_deadline_then_fresh_exact_request_heals(
    sessions, tmp_path: Path, response_shape: str
):
    """Catches transient redirect observations ending as permanent trust refusals."""
    from datetime import timedelta

    from vonk_control.lifecycle.model_cache import RECOVERY_BUDGET
    from vonk_control.models import ModelCacheOperation

    now = [NOW]
    healthy = [False]
    data = b"verified content after incomplete redirect observation"
    digest, _selector = _insert_model(sessions, _model(data))
    responses = []

    def handler(request):
        if healthy[0]:
            return httpx2.Response(200, request=request, content=data)
        headers = {"Location": CDN_URL} if response_shape == "cdn-redirect" else {}
        reply = httpx2.Response(302, request=request, headers=headers)
        responses.append(reply)
        return reply

    client = _client(handler)
    service = _service(sessions, tmp_path / response_shape, client)
    service._clock = lambda: now[0]
    try:
        _, original = _preview_and_start(service, digest, str(uuid4()))
        service.run_pending()
        assert responses and all(reply.is_closed for reply in responses)
        with sessions() as session:
            waiting = session.get(ModelCacheOperation, original.id)
            assert waiting is not None and waiting.completed_at is None
            assert waiting.lease_deadline is None and waiting.next_action_at is not None
        spec = service.resolve_artifact_set(model_content_sha256=digest).artifacts[0]
        assert not service._object_path(spec.sha256).exists()
        now[0] += RECOVERY_BUDGET + timedelta(seconds=1)
        service.run_pending()
        with sessions() as session:
            ended = session.get(ModelCacheOperation, original.id)
            assert ended is not None and ended.completed_at is not None
            assert ended.lease_deadline is None and ended.next_action_at is None
        healthy[0] = True
        _, fresh = _preview_and_start(service, digest, str(uuid4()))
        assert fresh.id != original.id
        service.run_pending()
        assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED
        assert service._object_path(spec.sha256).read_bytes() == data
    finally:
        service.close()
        client.close()
