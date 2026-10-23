"""Newest-only retention must release sets, not shared content or live owners."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from vonk_agent_protocol import InstallationState, LifecycleState, RunState
from vonk_control.auth import CursorCodec
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.catalog_revision_collection import CatalogRevisionCollector
from vonk_control.library_contract import LibraryCacheState
from vonk_control.library_projection import LibraryProjection
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache.errors import ModelCacheConflictUnknown
from vonk_control.model_cache.persistence import _manifest_of
from vonk_control.models import (
    CatalogDocument,
    CatalogDocumentRevision,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeInstallation,
    RecipeRun,
)
from vonk_forge_contracts import document_sha256

from .removal_settle import advance_until_ended
from .test_catalog_revision_collection import Catalog
from .test_model_cache import _artifact, _canonical_model, _download
from .test_model_removal_decided_by_evidence import _installation
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


def test_verified_successor_collects_only_old_unique_bytes(postgres_engine, tmp_path):
    """Catches keeping superseded sets forever or deleting shared/other quant bytes."""
    service, sessions = _removal_service(postgres_engine, tmp_path)
    try:
        old_digest, old, _ = _revision(service, sessions, tmp_path / "old")
        _, other, _ = _revision(
            service, sessions, tmp_path / "quant", slug="other-quant", data=b"qqq"
        )
        _, new, result = _revision(
            service, sessions, tmp_path / "new", data=b"new", note="new"
        )
        assert result.state == LifecycleState.SUCCEEDED
        _next_pass(service)
        assert not _contains(sessions, old)
        library = LibraryProjection(
            sessions,
            cursors=CursorCodec(b"x" * 32),
            model_storage=service.model_storage,
        )
        library.refresh_catalog()
        assert library._storage_observer._thread is not None
        library._storage_observer._thread.join(timeout=30)
        assert not library._storage_observer._thread.is_alive()
        assert (
            library._local(
                old_digest,
                kind="model",
                snapshot=library._database_local_state([old_digest]),
            ).controller
            == LibraryCacheState.NOT_CACHED
        )
        assert _contains(sessions, new) and _contains(sessions, other)
        assert not service._object_path(hashlib.sha256(b"old").hexdigest()).exists()
        assert (
            service._object_path(hashlib.sha256(b"shared").hexdigest()).read_bytes()
            == b"shared"
        )
    finally:
        service.close()


@pytest.mark.parametrize("running", [False, True])
def test_old_revision_survives_owner_then_is_released(
    postgres_engine, tmp_path, running
):
    """Catches deleting in-use bytes, or requiring another download after use ends."""
    service, sessions = _removal_service(postgres_engine, tmp_path)
    world = Catalog(postgres_engine)
    try:
        _, old, _ = _revision(service, sessions, tmp_path / "old")
        installation = _installation(world, old)
        with sessions.begin() as session:
            if running:
                installed = session.get(RecipeInstallation, installation)
                assert installed is not None
                installed.state = InstallationState.UNINSTALLED
                run = session.scalar(
                    select(RecipeRun).where(RecipeRun.installation_id == installation)
                )
                assert run is not None
                run.state = RunState.RUNNING
        _, new, _ = _revision(
            service, sessions, tmp_path / "new", data=b"new", note="new"
        )
        _next_pass(service)
        assert _contains(sessions, old)
        library = LibraryProjection(
            sessions,
            cursors=CursorCodec(b"x" * 32),
            model_storage=service.model_storage,
        )
        library.refresh_catalog()
        assert library._storage_observer._thread is not None
        library._storage_observer._thread.join(timeout=30)
        assert not library._storage_observer._thread.is_alive()
        listed = library.models(cached=True).models
        assert len(listed) == 1
        usage = listed[0].cache_usage
        assert usage is not None
        assert usage.disk_bytes == 12 and usage.unique_bytes == 12
        assert usage.retained_revisions[0].owners
        assert usage.retained_revisions[0].unique_bytes == 3
        with sessions.begin() as session:
            installed = session.get(RecipeInstallation, installation)
            assert installed is not None
            installed.state = InstallationState.UNINSTALLED
            for run in session.scalars(
                select(RecipeRun).where(RecipeRun.installation_id == installation)
            ):
                run.state = RunState.STOPPED
        # Restart must rediscover the ended owner, without an in-memory queue
        # or another operator download request.
        root = service.root
        service.close()
        service = ModelCacheService(
            sessions, root, reserve_bytes=0, fixture_sources=True
        )
        _next_pass(service)
        assert not _contains(sessions, old) and _contains(sessions, new)
    finally:
        service.close()


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


def test_a_shared_set_is_released_only_after_each_model_has_a_successor(
    postgres_engine, tmp_path, monkeypatch
):
    """Catches deleting another model's identical set, or retaining it forever afterwards."""
    service, sessions = _removal_service(postgres_engine, tmp_path)
    _content_keys(service, monkeypatch)
    try:
        _, shared, _ = _revision(service, sessions, tmp_path / "shared", slug="aa")
        _, other, _ = _revision(service, sessions, tmp_path / "shared", slug="bb")
        assert shared == other
        _revision(
            service, sessions, tmp_path / "new-aa", slug="aa", note="new", data=b"new"
        )
        _next_pass(service)
        assert _contains(sessions, shared)
        library = LibraryProjection(
            sessions,
            cursors=CursorCodec(b"x" * 32),
            model_storage=service.model_storage,
        )
        library.refresh_catalog()
        assert library._storage_observer._thread is not None
        library._storage_observer._thread.join(timeout=30)
        assert not library._storage_observer._thread.is_alive()
        listed = library.models(cached=True).models
        assert {model.selector for model in listed} == {"test/aa", "test/bb"}
        assert sum(model.resources.disk_bytes or 0 for model in listed) == 12
        held = next(
            model.cache_usage for model in listed if model.selector == "test/aa"
        )
        assert held is not None
        assert held.retained_revisions[0].owners == ["cached model test/bb"]
        assert held.retained_revisions[0].unique_bytes == 0
        _revision(
            service, sessions, tmp_path / "new-bb", slug="bb", note="new", data=b"bbb"
        )
        _next_pass(service)
        assert not _contains(sessions, shared)
        assert not service._object_path(hashlib.sha256(b"old").hexdigest()).exists()
        assert service._object_path(hashlib.sha256(b"shared").hexdigest()).is_file()
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


def test_superseded_sets_sharing_old_objects_are_collected_as_one_cohort(
    postgres_engine, tmp_path
):
    """Catches each old set leaving its shared obsolete weight as an orphan."""
    service, sessions = _removal_service(postgres_engine, tmp_path)
    frozen = datetime.now(UTC)
    service._clock = lambda: frozen
    try:
        _, first, _ = _revision(service, sessions, tmp_path / "first")
        _, second, _ = _revision(
            service, sessions, tmp_path / "second", note="editorial revision"
        )
        _, current, _ = _revision(
            service, sessions, tmp_path / "current", note="new weights", data=b"new"
        )
        assert first != second
        _next_pass(service)
        assert not _contains(sessions, first) and not _contains(sessions, second)
        assert _contains(sessions, current)
        assert not service._object_path(hashlib.sha256(b"old").hexdigest()).exists()
        assert service._object_path(hashlib.sha256(b"shared").hexdigest()).is_file()
    finally:
        service.close()
