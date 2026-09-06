from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.model_cache import ModelCacheService, ModelCacheStorageError
from vonk_control.model_cache_contract import ModelCacheOperationProgress
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    ModelCacheOperation,
)
from vonk_forge_contracts import ModelDefinition, content_sha256

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)


def _database(tmp_path: Path):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _service(tmp_path: Path, sessions, *, maximum: int = 4, handler=None, clock=None):
    client = None
    if handler is not None:
        client = httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
    service = ModelCacheService(
        sessions,
        tmp_path / "cache",
        reserve_bytes=0,
        max_parallel_downloads=maximum,
        fixture_sources=True,
        http_client=client,
        clock=clock,
    )
    return service, client


def _artifact(index: str, data: bytes, *, host: str = "example.test") -> dict[str, object]:
    return {
        "id": f"weights-{index}",
        "path": f"weights-{index}.bin",
        "kind": "http.file",
        "source": f"https://{host}/weights-{index}",
        "revision": "a" * 40,
        "sha256": hashlib.sha256(data).hexdigest(),
        "download_bytes": len(data),
        "roles": ["model"],
        "model_version_sha256": hashlib.sha256(index.encode()).hexdigest(),
    }


def _start(service: ModelCacheService, artifacts: list[dict[str, object]], key: str):
    model = str(artifacts[0]["model_version_sha256"])
    preview = service.download_preview(model_version_sha256=model, artifacts=artifacts)
    return service.start_download(
        actor="test",
        request_key=key,
        plan_digest=str(preview["plan_digest"]),
        model_version_sha256=model,
        artifacts=artifacts,
    )


def _drain(service: ModelCacheService, operation_id: str, *, limit: int = 200) -> None:
    for _ in range(limit):
        service.tick()
        if service.get_operation(operation_id).state in {"succeeded", "failed"}:
            return
        time.sleep(0.002)
    raise AssertionError("background operation did not settle")


def test_single_job_saturates_all_controller_transfer_slots(tmp_path: Path) -> None:
    sessions = _database(tmp_path)
    active = 0
    maximum = 0
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    responses = {
        f"/weights-{index}": f"payload-{index}".encode() for index in range(5)
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 4:
                started.set()
        release.wait(2)
        with lock:
            active -= 1
        data = responses[request.url.path]
        return httpx.Response(200, request=request, content=data)

    service, client = _service(tmp_path, sessions, handler=handler)
    artifacts = [_artifact(str(index), f"payload-{index}".encode()) for index in range(5)]
    operation = _start(
        service,
        artifacts,
        "00000000-0000-4000-8000-000000000301",
    )
    service.tick()
    assert started.wait(2)
    assert maximum == 4
    release.set()
    _drain(service, operation.id)
    assert service.get_operation(operation.id).state == "succeeded"
    service.close()
    assert client is not None and client.is_closed is False
    client.close()


def test_two_jobs_each_start_before_surplus_slots_are_round_robin_allocated(
    tmp_path: Path,
) -> None:
    sessions = _database(tmp_path)
    started: list[str] = []
    release = threading.Event()
    lock = threading.Lock()
    responses = {
        f"/weights-a{index}": f"a-{index}".encode() for index in range(3)
    } | {f"/weights-b{index}": f"b-{index}".encode() for index in range(3)}

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            started.append(request.url.path)
        release.wait(2)
        data = responses[request.url.path]
        return httpx.Response(200, request=request, content=data)

    service, client = _service(tmp_path, sessions, maximum=4, handler=handler)
    first = _start(
        service,
        [_artifact(f"a{index}", f"a-{index}".encode()) for index in range(3)],
        "00000000-0000-4000-8000-000000000302",
    )
    second = _start(
        service,
        [_artifact(f"b{index}", f"b-{index}".encode()) for index in range(3)],
        "00000000-0000-4000-8000-000000000303",
    )
    service.tick()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with lock:
            paths = tuple(started)
        if any(path.startswith("/weights-a0") for path in paths) and any(
            path.startswith("/weights-b0") for path in paths
        ):
            break
        time.sleep(0.005)
    with lock:
        assert any(path.startswith("/weights-a0") for path in started)
        assert any(path.startswith("/weights-b0") for path in started)
    release.set()
    _drain(service, first.id)
    _drain(service, second.id)
    assert service.get_operation(first.id).state == "succeeded"
    assert service.get_operation(second.id).state == "succeeded"
    service.close()
    assert client is not None
    client.close()


def test_failed_future_waits_for_sibling_before_finalizing_failure(tmp_path: Path) -> None:
    sessions = _database(tmp_path)
    sibling_started = threading.Event()
    release_sibling = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("fail"):
            return httpx.Response(200, request=request, content=b"wrongwrongwrong!")
        sibling_started.set()
        release_sibling.wait(2)
        return httpx.Response(200, request=request, content=b"sibling")

    service, client = _service(tmp_path, sessions, maximum=2, handler=handler)
    artifacts = [
        _artifact("fail", b"expected-failure"),
        _artifact("sibling", b"sibling"),
    ]
    operation = _start(
        service,
        artifacts,
        "00000000-0000-4000-8000-000000000304",
    )
    service.tick()
    assert sibling_started.wait(2)
    time.sleep(0.05)
    service.tick()
    assert service.get_operation(operation.id).state in {"running", "partial"}
    release_sibling.set()
    _drain(service, operation.id)
    final = service.get_operation(operation.id)
    assert final.state == "failed"
    assert final.failure is not None
    assert final.failure["code"] == "integrity_mismatch"
    assert final.progress["downloaded_bytes"] >= len(b"sibling")
    service.close()
    assert client is not None
    client.close()


def test_two_services_claim_distinct_operations_and_expired_lease_is_recovered(
    tmp_path: Path,
) -> None:
    sessions = _database(tmp_path)
    first, _ = _service(tmp_path, sessions, maximum=1)
    second, _ = _service(tmp_path, sessions, maximum=1)
    artifact_a = _artifact("claim-a", b"a")
    artifact_b = _artifact("claim-b", b"b")
    operation_a = _start(
        first,
        [artifact_a],
        "00000000-0000-4000-8000-000000000305",
    )
    operation_b = _start(
        first,
        [artifact_b],
        "00000000-0000-4000-8000-000000000306",
    )
    assert first._claim_operations(limit=1, respect_backoff=True) == [
        (operation_a.id, "download")
    ]
    assert second._claim_operations(limit=1, respect_backoff=True) == [
        (operation_b.id, "download")
    ]
    with sessions.begin() as session:
        row = session.get(ModelCacheOperation, operation_a.id)
        assert row is not None
        row.payload = dict(row.payload) | {
            "claim": {"owner": "dead-worker", "expires_at": "2020-01-01T00:00:00+00:00"}
        }
        row.state = "running"
    fresh, _ = _service(tmp_path, sessions, maximum=1)
    assert fresh._claim_operations(limit=1, respect_backoff=True) == [
        (operation_a.id, "download")
    ]
    first.close()
    second.close()
    fresh.close()


def test_postgres_concurrent_claims_are_distinct(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    first, _ = _service(tmp_path / "first", sessions, maximum=1)
    second, _ = _service(tmp_path / "second", sessions, maximum=1)
    operation_a = _start(
        first,
        [_artifact("pg-a", b"a")],
        "00000000-0000-4000-8000-000000000311",
    )
    operation_b = _start(
        first,
        [_artifact("pg-b", b"b")],
        "00000000-0000-4000-8000-000000000312",
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                lambda service: service._claim_operations(
                    limit=1, respect_backoff=True
                ),
                (first, second),
            )
        )
    claimed = {claim[0][0] for claim in claims}
    assert claimed == {operation_a.id, operation_b.id}
    first.close()
    second.close()


def test_hf_rate_limit_cooldown_survives_restart_but_local_work_progresses(
    tmp_path: Path,
) -> None:
    sessions = _database(tmp_path)
    now = [NOW]
    allow_hf = [False]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host or "")
        if request.url.host == "huggingface.co" and not allow_hf[0]:
            return httpx.Response(
                429,
                request=request,
                headers={"RateLimit": '"resolvers";r=0;t=30'},
            )
        content = b"hf" if request.url.host == "huggingface.co" else b"local"
        return httpx.Response(200, request=request, content=content)

    service, client = _service(
        tmp_path,
        sessions,
        maximum=1,
        handler=handler,
        clock=lambda: now[0],
    )
    hf = _artifact("hf", b"hf", host="huggingface.co")
    local = _artifact("local", b"local")
    hf["source"] = "https://huggingface.co/acme/model/resolve/" + "a" * 40 + "/weights-hf"
    first = _start(service, [hf], "00000000-0000-4000-8000-000000000307")
    service.tick()
    for _ in range(100):
        service.tick()
        if service.get_operation(first.id).state == "queued":
            break
        time.sleep(0.002)
    limited = service.get_operation(first.id)
    assert limited.state == "queued"
    assert limited.failure is not None
    assert limited.failure["retry_after_seconds"] == 30
    assert limited.failure["retry_time"] == "2026-09-06T12:00:30+00:00"
    service.close()
    if client is not None:
        client.close()

    restarted, client = _service(
        tmp_path,
        sessions,
        maximum=1,
        handler=handler,
        clock=lambda: now[0],
    )
    second = _start(restarted, [local], "00000000-0000-4000-8000-000000000308")
    restarted.tick()
    _drain(restarted, second.id)
    assert restarted.get_operation(second.id).state == "succeeded"
    assert calls.count("huggingface.co") == 1
    now[0] = NOW + timedelta(seconds=31)
    allow_hf[0] = True
    restarted.tick()
    _drain(restarted, first.id)
    assert restarted.get_operation(first.id).state == "succeeded"
    assert calls.count("huggingface.co") == 2
    restarted.close()
    if client is not None:
        client.close()


def test_progress_supports_more_than_128_members(tmp_path: Path) -> None:
    sessions = _database(tmp_path)
    service, _ = _service(tmp_path, sessions, maximum=1)
    artifacts = []
    for index in range(129):
        path = tmp_path / f"source-{index}"
        data = str(index).encode()
        path.write_bytes(data)
        artifacts.append(
            {
                "id": f"file-{index}",
                "path": f"file-{index}",
                "kind": "file",
                "source": path.as_uri(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "download_bytes": len(data),
                "roles": ["model"],
                "model_version_sha256": "c" * 64,
            }
        )
    operation = _start(
        service,
        artifacts,
        "00000000-0000-4000-8000-000000000309",
    )
    _drain(service, operation.id, limit=500)
    result = service.get_operation(operation.id)
    assert result.state == "succeeded"
    parsed = ModelCacheOperationProgress.model_validate(result.progress)
    assert len(parsed.members) == 129
    service.close()


def _model_document(slug: str, digest_byte: str, *, supersedes: str | None = None) -> dict[str, object]:
    source = resources.files("vonk_forge_contracts").joinpath("examples/model-definition.json")
    document = copy.deepcopy(json.loads(source.read_text(encoding="utf-8")))
    document["identity"]["publisher"] = "upstream"
    document["identity"]["slug"] = slug
    document["identity"]["model"]["publisher"] = "logical"
    document["identity"]["model"]["slug"] = "stable"
    document["identity"]["variant"] = "fp8"
    document["format"]["precision"] = "fp8"
    document["source"]["repository"] = "https://huggingface.co/acme/model"
    document["source"]["revision"] = digest_byte * 40
    document["files"][0]["sha256"] = digest_byte * 64
    document["files"][0]["size_bytes"] = 1
    validated = ModelDefinition.model_validate(document)
    canonical = validated.model_dump(mode="json")
    if supersedes is not None:
        canonical["_supersedes"] = supersedes
    return canonical


def _insert_model_revision(sessions, document: dict[str, object], *, created_at: datetime) -> str:
    supersedes = document.pop("_supersedes", None)
    digest = content_sha256(ModelDefinition.model_validate(document))
    identity = document["identity"]
    assert isinstance(identity, dict)
    root_id = f"00000000-0000-4000-8000-{digest[:12]}"
    with sessions.begin() as session:
        session.add(
            CatalogDocument(
                id=root_id,
                kind="model",
                publisher=str(identity["publisher"]),
                slug=str(identity["slug"]),
                title=str(identity["slug"]),
                created_by="test",
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.add(
            CatalogDocumentRevision(
                id=f"00000000-0000-4000-8000-{digest[12:24]}",
                document_id=root_id,
                kind="model",
                publisher=str(identity["publisher"]),
                slug=str(identity["slug"]),
                revision_number=1,
                schema_version=2,
                state="active",
                document=document,
                content_digest=digest,
                projected=(
                    {"supersedes": supersedes}
                    if isinstance(supersedes, str)
                    else {}
                ),
                created_by="test",
                created_at=created_at,
            )
        )
    return digest


def test_update_discovery_uses_nested_lineage_and_explicit_supersedes(tmp_path: Path) -> None:
    sessions = _database(tmp_path)
    service, _ = _service(tmp_path, sessions)
    current_doc = _model_document("source-revision-1", "1")
    current_digest = _insert_model_revision(sessions, current_doc, created_at=NOW)
    manifest = service.resolve_artifact_set(model_version_sha256=current_digest)
    with sessions.begin() as session:
        service._ensure_set(session, manifest)
    newer = _model_document("source-revision-2", "2", supersedes=current_digest)
    _insert_model_revision(sessions, newer, created_at=NOW + timedelta(hours=1))
    update = service.discover_updates(artifact_set_sha256=manifest.digest)["updates"][0]
    assert update["model_update_available"] is True
    assert update["model_update_from"]["content_sha256"] == current_digest
    assert update["model_update_to"]["publisher"] == "upstream"
    assert update["model_update_to"]["slug"] == "source-revision-2"
    service.close()


def test_update_discovery_reports_incomparable_lineage_candidates(tmp_path: Path) -> None:
    sessions = _database(tmp_path)
    service, _ = _service(tmp_path, sessions)
    current_doc = _model_document("source-revision-1", "3")
    current_digest = _insert_model_revision(sessions, current_doc, created_at=NOW)
    manifest = service.resolve_artifact_set(model_version_sha256=current_digest)
    with sessions.begin() as session:
        service._ensure_set(session, manifest)
    _insert_model_revision(sessions, _model_document("candidate-a", "4"), created_at=NOW + timedelta(hours=1))
    _insert_model_revision(sessions, _model_document("candidate-b", "5"), created_at=NOW + timedelta(hours=2))
    update = service.discover_updates(artifact_set_sha256=manifest.digest)["updates"][0]
    assert update["model_update_available"] is False
    assert update["model_update_ambiguous"] is True
    assert {item["slug"] for item in update["model_update_candidates"]} == {"candidate-a", "candidate-b"}
    service.close()


def test_close_shuts_down_controller_transfer_pool(tmp_path: Path) -> None:
    sessions = _database(tmp_path)
    service, _ = _service(tmp_path, sessions)
    assert service._executor._shutdown is False
    service.close()
    assert service._executor._shutdown is True


def test_close_checkpoints_active_transfer_and_fresh_service_resumes(
    tmp_path: Path,
) -> None:
    sessions = _database(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def blocking_handler(request: httpx.Request) -> httpx.Response:
        started.set()
        release.wait(2)
        return httpx.Response(200, request=request, content=b"resume-me")

    service, client = _service(
        tmp_path,
        sessions,
        maximum=1,
        handler=blocking_handler,
    )
    artifact = _artifact("shutdown", b"resume-me")
    operation = _start(
        service,
        [artifact],
        "00000000-0000-4000-8000-000000000310",
    )
    service.tick()
    assert started.wait(2)
    closer = threading.Thread(target=service.close)
    closer.start()
    time.sleep(0.05)
    release.set()
    closer.join(3)
    assert not closer.is_alive()
    assert service.get_operation(operation.id).state == "partial"

    def resumed_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"resume-me")

    fresh, fresh_client = _service(
        tmp_path,
        sessions,
        maximum=1,
        handler=resumed_handler,
    )
    _drain(fresh, operation.id)
    assert fresh.get_operation(operation.id).state == "succeeded"
    fresh.close()
    assert client is not None and fresh_client is not None
    client.close()
    fresh_client.close()


def test_terminal_hf_access_failure_requires_explicit_recheck_and_resume(
    tmp_path: Path,
) -> None:
    sessions = _database(tmp_path)
    token_path = tmp_path / "hf-token"
    token_path.write_text("bad-token\n")
    requests: list[httpx.Request] = []
    public_data = b"public model"
    hf_data = b"gated model"
    now = [NOW]
    rate_limit_once = [True]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("weights-z-hf") and request.headers.get(
            "authorization"
        ) == "Bearer bad-token":
            return httpx.Response(403, request=request)
        if request.url.path.endswith("weights-z-hf") and rate_limit_once[0]:
            rate_limit_once[0] = False
            return httpx.Response(
                429,
                request=request,
                headers={"RateLimit": '"resolvers";r=0;t=30'},
            )
        content = public_data if request.url.path.endswith("weights-a-public") else hf_data
        return httpx.Response(200, request=request, content=content)

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    service = ModelCacheService(
        sessions,
        tmp_path / "cache",
        reserve_bytes=0,
        max_parallel_downloads=1,
        fixture_sources=True,
        http_client=client,
        huggingface_token_path=token_path,
        clock=lambda: now[0],
    )
    artifacts = [
        _artifact("a-public", public_data, host="huggingface.co")
        | {
            "source": "https://huggingface.co/acme/public/resolve/"
            + "a" * 40
            + "/weights-a-public"
        },
        _artifact("z-hf", hf_data, host="huggingface.co")
        | {
            "source": "https://huggingface.co/acme/private/resolve/"
            + "a" * 40
            + "/weights-z-hf"
        },
    ]
    first = _start(
        service,
        artifacts,
        "00000000-0000-4000-8000-000000000401",
    )
    service.tick()
    _drain(service, first.id)
    failed = service.get_operation(first.id)
    assert failed.state == "failed"
    assert failed.failure is not None
    assert failed.failure["code"] == "access_denied"
    assert set(failed.failure) == {
        "code",
        "detail",
        "recovery_actions",
        "retryable",
        "retry_time",
        "retry_after_seconds",
        "log_excerpt",
        "required_bytes",
        "free_bytes",
        "shortfall_bytes",
    }
    assert "check_access_and_resume" in failed.failure["recovery_actions"]
    assert failed.progress["downloaded_bytes"] >= len(public_data)
    assert len(requests) == 2
    with sessions() as session:
        persisted = session.get(ModelCacheOperation, first.id)
        assert persisted is not None
        assert persisted.payload["failure"]["artifact_key"].endswith("z-hf")

    # Terminal auth failures do not re-enter the automatic scheduler.
    service.tick()
    assert len(requests) == 2

    denied = service.check_access_and_resume(
        first.id,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000402",
        artifact_set_sha256=str(first.artifact_set_sha256),
        plan_digest=str(first.plan_digest),
    )
    assert denied.id == first.id
    assert denied.state == "failed"
    assert len(requests) == 3
    denied_repeat = service.check_access_and_resume(
        first.id,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000402",
        artifact_set_sha256=str(first.artifact_set_sha256),
        plan_digest=str(first.plan_digest),
    )
    assert denied_repeat.id == first.id
    assert len(requests) == 3

    token_path.write_text("good-token\n")
    resumed = service.check_access_and_resume(
        first.id,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000403",
        artifact_set_sha256=str(first.artifact_set_sha256),
        plan_digest=str(first.plan_digest),
    )
    assert resumed.id == first.id
    assert resumed.state == "queued"
    assert resumed.failure is not None
    assert resumed.failure["code"] == "rate_limited"
    assert resumed.failure["retryable"] is True
    assert resumed.failure["retry_after_seconds"] == 30
    assert resumed.failure["recovery_actions"] == ["resume"]
    assert resumed.artifact_set_sha256 == first.artifact_set_sha256
    assert resumed.plan_digest == first.plan_digest
    assert resumed.progress["downloaded_bytes"] >= len(public_data)
    now[0] = NOW + timedelta(seconds=31)
    _drain(service, resumed.id)
    assert service.get_operation(resumed.id).state == "succeeded"
    assert len(requests) == 5
    service.close()
    client.close()


def test_access_recheck_groups_hf_files_by_repository_without_failed_key(
    tmp_path: Path,
) -> None:
    sessions = _database(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, content=b"ok")

    service, client = _service(tmp_path, sessions, handler=handler)
    artifacts = []
    for index in range(3):
        repo = "public" if index < 2 else "dependency"
        artifact = _artifact(
            f"file-{index}", f"ok-{index}".encode(), host="huggingface.co"
        )
        artifact["source"] = (
            f"https://huggingface.co/acme/{repo}/resolve/"
            + "a" * 40
            + f"/weights-{index}"
        )
        artifacts.append(artifact)
    manifest = service.resolve_artifact_set(
        model_version_sha256="e" * 64,
        artifacts=artifacts,
    )
    service._check_huggingface_access(manifest, failed_artifact_key=None)
    assert len(requests) == 2
    assert {request.url.path.split("/")[2] for request in requests} == {
        "public",
        "dependency",
    }
    service.close()
    assert client is not None
    client.close()


def test_failed_model_cache_detail_redacts_signed_source_url(tmp_path: Path) -> None:
    sessions = _database(tmp_path)
    service, _ = _service(tmp_path, sessions, maximum=1)
    artifact = _artifact("signed", b"payload")
    operation = _start(
        service,
        [artifact],
        "00000000-0000-4000-8000-000000000404",
    )
    manifest = service.manifest_for_artifact_set(str(operation.artifact_set_sha256))
    service._finish_failed(
        operation.id,
        str(operation.artifact_set_sha256),
        manifest,
        ModelCacheStorageError(
            "model_cache.source_unavailable",
            "failed at https://cdn.example/model?Signature=signed-download-secret#fragment-secret",
        ),
    )
    failed = service.get_operation(operation.id)
    assert failed.last_error is not None
    assert "signed-download-secret" not in failed.last_error
    assert "fragment-secret" not in failed.last_error
    assert failed.failure is not None
    assert "signed-download-secret" not in str(failed.failure["detail"])
    service.close()
