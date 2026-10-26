"""A cached model revision is kept until space is short, never released early."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from vonk_agent_protocol import LifecycleState
from vonk_control.auth import CursorCodec
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.catalog_revision_collection import CatalogRevisionCollector
from vonk_control.library_projection import LibraryProjection
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache.errors import ModelCacheConflictUnknown
from vonk_control.model_cache.persistence import _manifest_of
from vonk_control.models import (
    CatalogDocument,
    CatalogDocumentRevision,
    ModelCacheOperation,
    ModelCacheSet,
)
from vonk_forge_contracts import document_sha256

from .removal_settle import advance_until_ended
from .test_model_cache import _artifact, _canonical_model, _download
from .test_model_removal_reference_lifecycle import _register_model, _removal_service


def _revision(
    service,
    sessions,
    root: Path,
    *,
    slug="newest-only",
    data=b"old",
    note="old",
    fail=False,
):
    model = _canonical_model(
        publisher="test",
        slug=slug,
        file_id="weights",
        file_digest=hashlib.sha256(data).hexdigest(),
    )
    model = model.model_copy(
        update={"metadata": model.metadata.model_copy(update={"description": note})}
    )
    with sessions() as session:
        existing = session.scalar(
            select(CatalogDocument.id).where(
                CatalogDocument.publisher == "test", CatalogDocument.slug == slug
            )
        )
    if existing is None:
        _register_model(sessions, model)
    else:
        catalog = CatalogEntityService(sessions, clock=lambda: datetime.now(UTC))
        revised = catalog.revise(existing, model.model_dump(mode="json"), actor="test")
        catalog.resolve(revised.id, actor="test")
    digest = document_sha256(model.model_dump(mode="json"))
    root.mkdir(exist_ok=True)
    artifacts = [
        _artifact(root, data, model_content_sha256=digest),
        _artifact(
            root,
            b"shared",
            artifact_id="tokenizer",
            path="tokenizer.json",
            model_content_sha256=digest,
        ),
    ]
    if fail:
        (root / "weights.source").write_bytes(b"bad")
    operation = _download(
        service, artifacts, model_content_sha256=digest, request_key=str(uuid4())
    )
    assert operation.artifact_set_sha256 is not None
    return digest, operation.artifact_set_sha256, operation


def _contains(sessions, set_digest):
    with sessions() as session:
        return session.get(ModelCacheSet, set_digest) is not None


def _next_pass(service):
    clock = service._clock
    now = clock() + timedelta(minutes=1)
    service._clock = lambda: now
    service.run_pending()
    advance_until_ended(service)


def test_failed_successor_keeps_last_verified_revision(postgres_engine, tmp_path):
    """Catches releasing old content at admission rather than after verification."""
    service, sessions = _removal_service(postgres_engine, tmp_path)
    try:
        _, old, _ = _revision(service, sessions, tmp_path / "old")
        _, failed, result = _revision(
            service, sessions, tmp_path / "bad", data=b"new", note="bad", fail=True
        )
        assert result.state != LifecycleState.SUCCEEDED
        assert result.last_error is not None
        # Losing request history must not let an older verified set supersede
        # a newer partial set; only a strictly newer verified revision can.
        with sessions.begin() as session:
            session.execute(delete(ModelCacheOperation))
        _next_pass(service)
        assert _contains(sessions, failed)
        assert _contains(sessions, old)
        assert (
            service._object_path(hashlib.sha256(b"old").hexdigest()).read_bytes()
            == b"old"
        )
    finally:
        service.close()


def _content_keys(service, monkeypatch):
    """Use reusable keys, as catalog ingress does, rather than fixture provenance keys."""
    original = service._artifact_from_input

    def content_key(value, *, model_content_sha256):
        spec = original(value, model_content_sha256=model_content_sha256)
        return replace(spec, key=spec.artifact_id)

    monkeypatch.setattr(service, "_artifact_from_input", content_key)


def test_metadata_only_revision_lists_one_copy_with_real_bytes(
    postgres_engine, tmp_path, monkeypatch
):
    """Catches billing identical files twice or hiding the new provenance's bytes."""
    service, sessions = _removal_service(postgres_engine, tmp_path)
    _content_keys(service, monkeypatch)
    try:
        _, old, _ = _revision(service, sessions, tmp_path / "same")
        digest, new, _ = _revision(
            service, sessions, tmp_path / "same", note="new metadata"
        )
        assert old == new
        # Request history is disposable; artifact-owned verified references
        # must survive its collection and a Controller restart.
        with sessions.begin() as session:
            session.execute(delete(ModelCacheOperation))
        root = service.root
        service.close()
        service = ModelCacheService(
            sessions, root, reserve_bytes=0, fixture_sources=True
        )
        library = LibraryProjection(
            sessions,
            cursors=CursorCodec(b"x" * 32),
            model_storage=service.model_storage,
        )
        library.refresh_catalog()
        assert library._storage_observer._thread is not None
        library._storage_observer._thread.join(timeout=30)
        assert not library._storage_observer._thread.is_alive()
        rows = library.models(cached=True).models
        assert len(rows) == 1
        assert rows[0].identity.content_sha256 == digest
        assert rows[0].resources.disk_bytes == 9
        _next_pass(service)
        assert _contains(sessions, new)
    finally:
        service.close()


def test_shared_objects_are_charged_once_even_with_filters(postgres_engine, tmp_path):
    """Catches per-document totals or accounting that changes with page/filter order."""
    service, sessions = _removal_service(postgres_engine, tmp_path)
    try:
        _revision(service, sessions, tmp_path / "a", slug="aa")
        _revision(service, sessions, tmp_path / "b", slug="bb", data=b"new")
        library = LibraryProjection(
            sessions,
            cursors=CursorCodec(b"x" * 32),
            model_storage=service.model_storage,
        )
        library.refresh_catalog()
        assert library._storage_observer._thread is not None
        library._storage_observer._thread.join(timeout=30)
        assert not library._storage_observer._thread.is_alive()
        rows = library.models(cached=True).models
        assert sum(row.resources.disk_bytes or 0 for row in rows) == 12
        for row in rows:
            usage = row.cache_usage
            assert usage is not None
            assert usage.unique_bytes == 3 and usage.shared_bytes == 6
        filtered = library.models(cached=True, search="test/bb").models
        assert len(filtered) == 1
        assert filtered[0].resources == next(
            row.resources for row in rows if row.selector == "test/bb"
        )
    finally:
        service.close()


def test_completed_download_repairs_lost_revision_binding_after_restart(
    postgres_engine, tmp_path, monkeypatch
):
    """Catches requiring a new request when verified bytes outlive binding publication."""
    service, sessions = _removal_service(postgres_engine, tmp_path)
    _content_keys(service, monkeypatch)
    try:
        # Inject the missing publication through the real download/store path.
        monkeypatch.setattr(
            service, "_remember_verified_models", lambda session, row, manifest: None
        )
        _revision(service, sessions, tmp_path / "same")
        digest, _, _ = _revision(
            service, sessions, tmp_path / "same", note="new metadata"
        )
        root = service.root
        service.close()
        service = ModelCacheService(
            sessions, root, reserve_bytes=0, fixture_sources=True
        )
        _next_pass(service)
        library = LibraryProjection(
            sessions,
            cursors=CursorCodec(b"x" * 32),
            model_storage=service.model_storage,
        )
        library.refresh_catalog()
        assert library._storage_observer._thread is not None
        library._storage_observer._thread.join(timeout=30)
        assert not library._storage_observer._thread.is_alive()
        rows = library.models(cached=True).models
        assert len(rows) == 1 and rows[0].identity.content_sha256 == digest
        assert rows[0].resources.disk_bytes == 9
    finally:
        service.close()


def test_cache_bindings_keep_their_model_metadata_until_replacement(
    postgres_engine, tmp_path, monkeypatch
):
    """Catches model-definition collection making retained cache bytes unselectable."""
    service, sessions = _removal_service(postgres_engine, tmp_path)
    _content_keys(service, monkeypatch)
    try:
        _revision(service, sessions, tmp_path / "same")
        digest, _, _ = _revision(
            service, sessions, tmp_path / "same", note="new metadata"
        )
        _revision(
            service,
            sessions,
            tmp_path / "failed",
            note="failed successor",
            data=b"new",
            fail=True,
        )
        with sessions.begin() as session:
            session.execute(delete(ModelCacheOperation))
        now = datetime.now(UTC) + timedelta(days=3)
        CatalogRevisionCollector(sessions, clock=lambda: now).collect()
        with sessions() as session:
            assert (
                session.scalar(
                    select(CatalogDocumentRevision.id).where(
                        CatalogDocumentRevision.content_digest == digest
                    )
                )
                is not None
            )
        library = LibraryProjection(
            sessions,
            cursors=CursorCodec(b"x" * 32),
            model_storage=service.model_storage,
        )
        library.refresh_catalog()
        assert library._storage_observer._thread is not None
        library._storage_observer._thread.join(timeout=30)
        assert not library._storage_observer._thread.is_alive()
        rows = library.models(cached=True).models
        assert len(rows) == 1 and rows[0].identity.content_sha256 == digest
    finally:
        service.close()


def test_busy_binding_publisher_replans_and_preserves_both_models(
    postgres_engine, tmp_path, monkeypatch
):
    """Real overlapping PostgreSQL transactions catch lost verified-reference updates."""
    service, sessions = _removal_service(postgres_engine, tmp_path)
    _content_keys(service, monkeypatch)
    try:
        _, shared, first = _revision(service, sessions, tmp_path / "same", slug="aa")
        _revision(service, sessions, tmp_path / "same", slug="bb")
        with sessions.begin() as session:
            row = session.get(ModelCacheSet, shared)
            assert row is not None
            manifest = service._stored_manifest(row)
            assert manifest is not None
            row.manifest = replace(manifest, verified_models=()).document()
            operation = session.get(ModelCacheOperation, first.id)
            payload = (
                service._transfer_or_none(operation) if operation is not None else None
            )
            assert payload is not None
            incoming = _manifest_of(payload)
        with sessions.begin() as holder:
            row = holder.get(ModelCacheSet, shared)
            assert row is not None
            service._remember_verified_models(holder, row, incoming)
            with (
                pytest.raises(ModelCacheConflictUnknown),
                sessions.begin() as contender,
            ):
                other = contender.get(ModelCacheSet, shared)
                assert other is not None
                service._remember_verified_models(contender, other, incoming)
        _next_pass(service)
        with sessions() as session:
            row = session.get(ModelCacheSet, shared)
            assert row is not None
            stored = service._stored_manifest(row)
            assert stored is not None
            assert {reference.slug for reference in stored.verified_models} == {
                "aa",
                "bb",
            }
    finally:
        service.close()
