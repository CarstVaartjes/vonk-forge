"""Shared helpers for the model_cache tests."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_forge_contracts import ModelDefinition
from vonk_forge_contracts.model import ModelReference

from vonk_control.bounded_json import text
from vonk_control.model_cache.artifacts import ArtifactSetManifest, ArtifactSpec
from vonk_control.model_cache.service import ModelCacheService
from vonk_control.model_primitives import Base

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def _required_text(value: object, detail: str) -> str:
    """Return *value* as text or fail loudly at the fixture boundary."""

    result = text(value)
    assert result is not None, detail
    return result


def _first_manifest_artifact(document: dict[str, object]) -> dict[str, object]:
    """Return a mutable copy of the first artifact of a manifest document."""

    artifacts = document["artifacts"]
    assert isinstance(artifacts, list)
    assert artifacts
    first = artifacts[0]
    assert isinstance(first, dict)
    return dict(first)


def _canonical_model(
    *,
    publisher: str,
    slug: str,
    file_id: str,
    file_digest: str,
    dependencies: list[dict[str, str]] | None = None,
) -> ModelDefinition:
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    document["identity"]["publisher"] = publisher
    document["identity"]["slug"] = slug
    document["identity"]["model"]["publisher"] = publisher
    document["identity"]["model"]["slug"] = slug
    document["source"] = {
        "repository": f"https://huggingface.co/{publisher}/{slug}",
        "revision": "0" * 40,
    }
    document["files"] = [
        {
            "id": file_id,
            "path": f"{file_id}.safetensors",
            "sha256": file_digest,
            "size_bytes": 3,
            "roles": ["weights"],
        }
    ]
    document["dependencies"] = dependencies or []
    return ModelDefinition.model_validate(document)


def _canonical_recipe(model_digest: str) -> dict[str, object]:
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text(encoding="utf-8")
    )
    document["models"][0]["model"]["content_sha256"] = model_digest
    return document


def _shared_connection_sessions():
    """Sessions whose every thread shares one sqlite connection."""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def _watch_services_on(sessions):
    """Close every ``ModelCacheService`` built on ``sessions``; fail if any
    started background work.

    One connection serves every thread there, so a thread that touches the
    database can roll back another's open transaction ("cannot commit - no
    transaction is active").  A service that ticks or schedules a transfer
    starts pool threads (their entries persist after shutdown), and belongs on
    ``threaded_cache``.  A second service built on the same sessions (a
    restart) counts as much as the fixture's own.
    """

    built: list[ModelCacheService] = []
    original = ModelCacheService.__init__

    def record(self, session_factory, *args, **kwargs):
        original(self, session_factory, *args, **kwargs)
        if session_factory is sessions:
            built.append(self)

    ModelCacheService.__init__ = record  # type: ignore[method-assign]
    try:
        yield
    finally:
        ModelCacheService.__init__ = original  # type: ignore[method-assign]
        started = [
            service
            for service in built
            if service._storage_observer._pool._threads or service._executor._threads
        ]
        for service in built:
            service.close()
        assert not started, (
            "started background work on the shared-connection fixture; "
            "use threaded_cache"
        )


@pytest.fixture
def cache(tmp_path: Path):
    sessions = _shared_connection_sessions()
    runtime_root = tmp_path / "runtime-images"
    runtime_root.mkdir()

    def runtime_archive_available(digest: str, expected_bytes: int) -> bool:
        path = runtime_root / digest
        return (
            path.is_file()
            and not path.is_symlink()
            and path.stat().st_size == expected_bytes
        )

    with _watch_services_on(sessions):
        service = ModelCacheService(
            sessions,
            tmp_path / "nas-cache",
            free_fraction=0,
            fixture_sources=True,
            runtime_archive_available=runtime_archive_available,
        )
        yield service, sessions


def _artifact(
    root: Path,
    data: bytes,
    *,
    artifact_id: str = "weights",
    path: str = "weights.bin",
    model_content_sha256: str = "a" * 64,
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
        "model_content_sha256": model_content_sha256,
    }


@pytest.fixture
def threaded_cache(tmp_path: Path):
    """Background workers use independent connections, like the Controller."""

    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'background-cache.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = ModelCacheService(
        sessions,
        tmp_path / "background-nas-cache",
        free_fraction=0,
        fixture_sources=True,
    )
    try:
        yield service, sessions
    finally:
        service.close()
        engine.dispose()


def _download(
    service: ModelCacheService,
    artifacts: list[dict[str, object]],
    *,
    model_content_sha256: str,
    request_key: str,
    interrupt_after_bytes: int | None = None,
):
    operation = service.start_download(
        actor="test",
        request_key=request_key,
        model_content_sha256=model_content_sha256,
        artifacts=artifacts,
        interrupt_after_bytes=interrupt_after_bytes,
    )
    if interrupt_after_bytes is None:
        service.run_pending()
        operation = service.get_operation(operation.id)
    return operation


def _remove_model(
    service: ModelCacheService,
    selector: str,
    *,
    actor: str,
    request_key: str,
    model_content_sha256: str,
):
    return service.remove_model_selector(
        selector,
        actor=actor,
        request_key=request_key,
    )


def _manifest_document(tmp_path: Path) -> dict[str, object]:
    data = b"manifest bytes"
    source = tmp_path / "manifest.source"
    source.write_bytes(data)
    artifact = ArtifactSpec(
        key="weights",
        artifact_id="weights",
        path="weights.bin",
        kind="file",
        repository=None,
        source=source.as_uri(),
        revision=None,
        sha256=hashlib.sha256(data).hexdigest(),
        expected_bytes=len(data),
        roles=("model",),
        model_content_sha256="a" * 64,
    )
    return ArtifactSetManifest(
        model_content_sha256="a" * 64,
        recipe_revision_sha256=None,
        model_content_digests=("a" * 64,),
        artifacts=(artifact,),
        model_definition_ref=ModelReference(
            publisher="vonk-forge", slug="manifest-model", content_sha256="a" * 64
        ),
    ).document()


def _http_artifact(payload: bytes, *, model: str = "b" * 64) -> dict[str, object]:
    return {
        "id": "weights",
        "path": "weights.bin",
        "kind": "http.file",
        "source": "https://example.test/weights.bin",
        "revision": "a" * 40,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "download_bytes": len(payload),
        "roles": ["model"],
        "model_content_sha256": model,
    }


def _http_cache_service(tmp_path: Path, sessions, handler, *, clock=None):
    client = httpx2.Client(
        transport=httpx2.MockTransport(handler),
        follow_redirects=False,
    )
    service = ModelCacheService(
        sessions,
        tmp_path / "http-nas-cache",
        free_fraction=0,
        http_client=client,
        fixture_sources=True,
        clock=clock,
    )
    return service, client


def _gone_handler(statuses: list[int]):
    """Answer each request with the next status; the last one repeats."""

    served = {"count": 0}
    payload = b"source gone payload"

    def handler(request: httpx2.Request) -> httpx2.Response:
        index = min(served["count"], len(statuses) - 1)
        served["count"] += 1
        if statuses[index] == 200:
            return httpx2.Response(200, request=request, content=payload)
        return httpx2.Response(statuses[index], request=request)

    return handler, payload, served


def _admit_fresh_download(service, model_digest, artifact):
    return service.start_download(
        actor="test",
        request_key=str(uuid.uuid4()),
        model_content_sha256=model_digest,
        artifacts=[artifact],
    )
