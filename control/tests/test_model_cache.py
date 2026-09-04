from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.auth import Actor
from vonk_control.model_cache import (
    ModelCacheConflict,
    ModelCacheService,
)
from vonk_control.model_cache_api import install_model_cache_routes
from vonk_control.model_cache_contract import (
    ModelCacheDownloadRequest,
    ModelCacheEvictionPreviewRequest,
    ModelCacheEvictRequest,
)
from vonk_control.models import Base, FleetProfile, ModelCacheArtifact
from vonk_control.worker import Worker

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


@pytest.fixture
def cache(tmp_path: Path):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = ModelCacheService(sessions, tmp_path / "nas-cache", reserve_bytes=0, fixture_sources=True)
    return service, sessions


def _artifact(
    root: Path,
    data: bytes,
    *,
    artifact_id: str = "weights",
    path: str = "weights.bin",
    model_version_sha256: str = "a" * 64,
) -> dict[str, object]:
    source = root / f"{artifact_id}.source"
    source.write_bytes(data)
    return {
        "id": artifact_id,
        "path": path,
        "kind": "file",
        "source": source.as_uri(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "download_bytes": len(data),
        "roles": ["model" if artifact_id == "weights" else "auxiliary"],
        "model_version_sha256": model_version_sha256,
    }


def _download(
    service: ModelCacheService,
    artifacts: list[dict[str, object]],
    *,
    model_version_sha256: str,
    request_key: str,
    interrupt_after_bytes: int | None = None,
):
    preview = service.download_preview(
        model_version_sha256=model_version_sha256,
        artifacts=artifacts,
    )
    operation = service.start_download(
        actor="test",
        request_key=request_key,
        plan_digest=str(preview["plan_digest"]),
        model_version_sha256=model_version_sha256,
        artifacts=artifacts,
        interrupt_after_bytes=interrupt_after_bytes,
    )
    if interrupt_after_bytes is None:
        service.run_pending()
        operation = service.get_operation(operation.id)
    return operation


def test_download_persists_real_primary_and_auxiliary_bytes_and_deduplicates(
    cache, tmp_path: Path
) -> None:
    service, sessions = cache
    model_a = "a" * 64
    primary = _artifact(tmp_path, b"primary model bytes", model_version_sha256=model_a)
    auxiliary = _artifact(
        tmp_path,
        b"tokenizer auxiliary bytes",
        artifact_id="tokenizer",
        path="tokenizer.json",
        model_version_sha256=model_a,
    )

    first = _download(
        service,
        [primary, auxiliary],
        model_version_sha256=model_a,
        request_key="00000000-0000-4000-8000-000000000001",
    )
    assert first.state == "succeeded"
    entry = service.get_entry(first.artifact_set_sha256 or "")
    assert entry["coverage"] == "complete"
    assert entry["expected_bytes"] == len(b"primary model bytestokenizer auxiliary bytes")
    assert entry["verified_bytes"] == entry["expected_bytes"]
    assert {item["path"] for item in entry["artifacts"]} == {
        "weights.bin",
        "tokenizer.json",
    }
    preparation = service.preparation_evidence(first.artifact_set_sha256 or "")
    assert preparation["artifact_set_sha256"] == first.artifact_set_sha256
    assert preparation["artifact_set_bytes"] == entry["expected_bytes"]
    assert preparation["controller"]["state"] == "ready"
    assert preparation["controller"]["verified_sha256"] == first.artifact_set_sha256
    assert preparation["targets"] == []

    descriptors = service.resolve_verified_artifact_set(first.artifact_set_sha256 or "")
    assert {item["sha256"] for item in descriptors} == {
        primary["sha256"],
        auxiliary["sha256"],
    }
    assert service.read_verified_artifact(
        first.artifact_set_sha256 or "",
        str(primary["sha256"]),
        "weights.bin",
        offset=8,
        maximum_bytes=6,
    ) == b"model "

    model_b = "b" * 64
    primary_b = dict(primary, model_version_sha256=model_b)
    auxiliary_b = dict(auxiliary, model_version_sha256=model_b)
    second = _download(
        service,
        [primary_b, auxiliary_b],
        model_version_sha256=model_b,
        request_key="00000000-0000-4000-8000-000000000002",
    )
    assert second.state == "succeeded"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(ModelCacheArtifact)) == 2
    assert service.storage_summary().unique_used_bytes == len(
        b"primary model bytes"
    ) + len(b"tokenizer auxiliary bytes")


def test_download_mutation_is_queued_until_the_controller_worker_runs(
    cache, tmp_path: Path
) -> None:
    service, _sessions = cache
    model = "1" * 64
    data = b"queued payload"
    artifact = _artifact(tmp_path, data, model_version_sha256=model)
    preview = service.download_preview(
        model_version_sha256=model,
        artifacts=[artifact],
    )

    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000014",
        plan_digest=str(preview["plan_digest"]),
        model_version_sha256=model,
        artifacts=[artifact],
    )
    assert operation.state == "queued"
    assert not list(service.root.joinpath("objects").glob("*/*"))

    assert service.run_pending() == 1
    assert service.get_operation(operation.id).state == "succeeded"


def test_interrupted_download_checkpoint_resumes_after_service_restart(
    cache, tmp_path: Path
) -> None:
    service, sessions = cache
    model = "c" * 64
    data = bytes(range(256)) * 12_000
    artifact = _artifact(tmp_path, data, model_version_sha256=model)

    partial = _download(
        service,
        [artifact],
        model_version_sha256=model,
        request_key="00000000-0000-4000-8000-000000000003",
        interrupt_after_bytes=1_100_000,
    )
    assert partial.state == "partial"
    set_digest = partial.artifact_set_sha256 or ""
    part = service.root / "partials" / set_digest / f"{artifact['sha256']}.part"
    assert 0 < part.stat().st_size < len(data)

    restarted = ModelCacheService(
        sessions,
        service.root,
        reserve_bytes=0,
        fixture_sources=True,
    )
    assert restarted.resume_operations() == 1
    restarted.run_pending()
    resumed = restarted.get_operation(partial.id)
    assert resumed.state == "succeeded"
    assert (service.root / "objects" / str(artifact["sha256"])[0:2] / str(artifact["sha256"]).strip()).read_bytes() == data
    assert restarted.get_entry(set_digest)["coverage"] == "complete"


def test_same_pin_repair_verifies_before_atomic_replace_and_preserves_old_bytes(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _sessions = cache
    model = "d" * 64
    good = b"good payload"
    artifact = _artifact(tmp_path, good, artifact_id="weights", model_version_sha256=model)
    source = tmp_path / "weights.source"
    set_digest = _download(
        service,
        [artifact],
        model_version_sha256=model,
        request_key="00000000-0000-4000-8000-000000000004",
    ).artifact_set_sha256 or ""
    target = service.root / "objects" / str(artifact["sha256"])[0:2] / str(artifact["sha256"])
    assert target.read_bytes() == good

    source.write_bytes(b"bad! payload")
    bad_preview = service.repair_preview(set_digest)
    failed = service.start_repair(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000005",
        artifact_set_sha256=set_digest,
        plan_digest=str(bad_preview["plan_digest"]),
    )
    service.run_pending()
    failed = service.get_operation(failed.id)
    assert failed.state == "failed"
    assert target.read_bytes() == good

    source.write_bytes(good)
    replace = __import__("vonk_control.model_cache", fromlist=["os"]).os.replace
    calls = 0

    def fail_final_replace(source_path, target_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated atomic publish failure")
        return replace(source_path, target_path)

    monkeypatch.setattr("vonk_control.model_cache.os.replace", fail_final_replace)
    swap_failed = service.start_repair(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000006",
        artifact_set_sha256=set_digest,
        plan_digest=str(bad_preview["plan_digest"]),
    )
    service.run_pending()
    swap_failed = service.get_operation(swap_failed.id)
    assert swap_failed.state == "failed"
    assert target.read_bytes() == good
    assert not any(service.root.joinpath("quarantine").iterdir())

    monkeypatch.undo()
    repaired = service.start_repair(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000007",
        artifact_set_sha256=set_digest,
        plan_digest=str(bad_preview["plan_digest"]),
    )
    service.run_pending()
    repaired = service.get_operation(repaired.id)
    assert repaired.state == "succeeded"
    assert target.read_bytes() == good


def test_protection_is_derived_from_durable_references_and_blocks_eviction(
    cache, tmp_path: Path
) -> None:
    service, sessions = cache
    model = "e" * 64
    data = b"protected model"
    artifact = _artifact(tmp_path, data, model_version_sha256=model)
    set_digest = _download(
        service,
        [artifact],
        model_version_sha256=model,
        request_key="00000000-0000-4000-8000-000000000008",
    ).artifact_set_sha256 or ""
    with sessions.begin() as session:
        session.add(
            FleetProfile(
                name="protected-profile",
                description="",
                installation_policy="keep-cached",
                assignments=[{"model_version_sha256": model}],
                labels={},
                favorite=False,
                created_by="test",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    preview = service.eviction_preview(target_bytes=len(data))
    assert preview["selected"] == []
    assert preview["selected_bytes"] == 0
    assert preview["protected_entries"][0]["artifact_set_sha256"] == set_digest
    assert "protected entries require separate reference removal" in preview["blockers"]
    assert service.storage_summary().protected_bytes == len(data)
    with pytest.raises(ModelCacheConflict):
        service.evict(
            actor="test",
            request_key="00000000-0000-4000-8000-000000000009",
            plan_digest=str(preview["plan_digest"]),
            target_bytes=len(data),
        )

    with sessions.begin() as session:
        session.query(FleetProfile).delete()
    unprotected = service.eviction_preview(target_bytes=len(data))
    assert unprotected["blockers"] == []
    assert unprotected["selected_bytes"] == len(data)
    removed = service.evict(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000010",
        plan_digest=str(unprotected["plan_digest"]),
        target_bytes=len(data),
    )
    service.run_pending()
    removed = service.get_operation(removed.id)
    assert removed.state == "succeeded"
    assert service.storage_summary().unique_used_bytes == 0


def test_contracts_and_routes_are_schema_two_and_do_not_accept_sources_or_force_flags(
    cache,
) -> None:
    service, _sessions = cache
    assert "artifacts" not in ModelCacheDownloadRequest.model_fields
    assert "protected" not in ModelCacheEvictionPreviewRequest.model_fields
    assert set(ModelCacheDownloadRequest.model_fields) >= {"request_key", "plan_digest"}
    assert set(ModelCacheEvictRequest.model_fields) >= {"request_key", "plan_digest"}
    with pytest.raises(ValueError):
        ModelCacheDownloadRequest(
            request_key="00000000-0000-4000-8000-000000000011",
            plan_digest="f" * 64,
            artifacts=[],
        )

    app = FastAPI()
    install_model_cache_routes(
        app,
        actor_dependency=Depends(lambda: Actor("admin", "administrator")),
        service=service,
        audits=[],
    )
    client = TestClient(app)
    inventory = client.get("/api/v1/model-cache")
    assert inventory.status_code == 200
    assert inventory.json()["schema_version"] == 2
    assert inventory.json()["storage"]["unique_used_bytes"] == 0
    operations = client.get("/api/v1/model-cache/operations")
    assert operations.status_code == 200
    assert operations.json() == {
        "schema_version": 2,
        "operations": [],
        "total": 0,
        "next_cursor": None,
    }
    updates = client.get("/api/v1/model-cache/updates")
    assert updates.status_code == 200
    assert updates.json() == {
        "schema_version": 2,
        "source_policy": "nas-first",
        "updates": [],
        "total": 0,
        "next_cursor": None,
    }
    bad = client.post(
        "/api/v1/model-cache/download",
        json={
            "schema_version": 2,
            "request_key": "00000000-0000-4000-8000-000000000012",
            "plan_digest": "f" * 64,
            "model_version_sha256": "a" * 64,
            "artifacts": [{"source": "file:///etc/passwd"}],
            "protected": True,
        },
    )
    assert bad.status_code == 422
    assert {route.path for route in app.routes} >= {
        "/api/v1/model-cache",
        "/api/v1/model-cache/download-preview",
        "/api/v1/model-cache/download",
        "/api/v1/model-cache/repair-preview",
        "/api/v1/model-cache/repair",
        "/api/v1/model-cache/eviction-preview",
        "/api/v1/model-cache/evict",
        "/api/v1/model-cache/updates",
        "/api/v1/model-cache/operations",
        "/api/v1/model-cache/operations/{operation_id}",
    }


def test_verified_serving_seam_refuses_incomplete_or_tampered_sets(cache, tmp_path: Path) -> None:
    service, _sessions = cache
    model = "f" * 64
    data = b"bounded bytes"
    artifact = _artifact(tmp_path, data, model_version_sha256=model)
    interrupted = _download(
        service,
        [artifact],
        model_version_sha256=model,
        request_key="00000000-0000-4000-8000-000000000013",
        interrupt_after_bytes=1,
    )
    with pytest.raises(ModelCacheConflict, match="not completely verified"):
        service.resolve_verified_artifact_set(interrupted.artifact_set_sha256 or "")

    assert service.resume_operations() == 1
    service.run_pending()
    set_digest = interrupted.artifact_set_sha256 or ""
    target = service.root / "objects" / str(artifact["sha256"])[0:2] / str(artifact["sha256"])
    assert service.read_verified_artifact(set_digest, str(artifact["sha256"]), "weights.bin") == data
    target.write_bytes(b"tampered!!!")
    with pytest.raises(ModelCacheConflict, match="not completely verified"):
        service.read_verified_artifact(set_digest, str(artifact["sha256"]), "weights.bin")


def test_controller_worker_drains_queued_cache_operations_without_inline_api_transfer() -> None:
    calls: list[int] = []

    class Jobs:
        def claim(self, *_args, **_kwargs):
            raise AssertionError("cache work should be selected before generic jobs")

    class CacheWorker:
        def run_pending(self, *, limit: int) -> int:
            calls.append(limit)
            return 1

    worker = Worker(Jobs(), "worker", {}, model_cache=CacheWorker())
    assert worker.run_once() is True
    assert calls == [1]
