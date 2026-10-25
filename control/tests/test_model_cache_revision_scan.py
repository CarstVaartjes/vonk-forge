"""The model revision scan costs what changed in the catalog, not its size."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import CursorCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache.artifacts import _unique_artifacts
from vonk_control.models import Base, CatalogDocumentRevision, ModelCacheSet
from vonk_forge_contracts import document_sha256

from .test_model_cache import _artifact, _canonical_model, _download
from .test_model_removal_reference_lifecycle import _register_model

# Production carries about 300 current model heads, most of them not cached.
UNCACHED_MODELS = 120
# Past the longest jittered backoff before an unresolvable digest is observed again.
PAST_BACKOFF = timedelta(seconds=91)


def _model(sessions, slug: str, *, dependencies=None) -> str:
    model = _canonical_model(
        publisher="test",
        slug=slug,
        file_id="weights",
        file_digest=hashlib.sha256(slug.encode()).hexdigest(),
        dependencies=dependencies,
    )
    _register_model(sessions, model)
    return document_sha256(model.model_dump(mode="json"))


def _cache(tmp_path, now):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'cache.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = ModelCacheService(
        sessions,
        tmp_path / "cache",
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now[0],
    )
    return engine, sessions, service


def _counting_scans(service, monkeypatch):
    resolved: list[str | None] = []
    resolve_once = ModelCacheService._resolve_artifact_set_once

    def counting(self, **kwargs):
        resolved.append(kwargs.get("model_content_sha256"))
        return resolve_once(self, **kwargs)

    monkeypatch.setattr(ModelCacheService, "_resolve_artifact_set_once", counting)

    def scan() -> set[str | None]:
        resolved.clear()
        service._revision_scan_at = None
        service.reconcile_model_revisions()
        return set(resolved)

    return scan


def test_revision_scan_resolves_only_model_digests_the_catalog_changed(
    tmp_path, monkeypatch
) -> None:
    """Catches a scan that re-resolves every uncached model on every pass: at
    production scale the single worker loop outlives its watchdog and restarts,
    dropping every in-process transfer."""
    now = [datetime(2026, 10, 25, tzinfo=UTC)]
    engine, sessions, service = _cache(tmp_path, now)
    try:
        cached = _model(sessions, "cached")
        _download(
            service,
            [_artifact(tmp_path, b"abc", model_content_sha256=cached)],
            model_content_sha256=cached,
            request_key=str(uuid4()),
        )
        uncached = {_model(sessions, f"uncached-{i}") for i in range(UNCACHED_MODELS)}
        # A head whose dependency the catalog does not hold never resolves.
        unresolvable = _model(
            sessions,
            "missing-dependency",
            dependencies=[
                {"publisher": "test", "slug": "absent", "content_sha256": "e" * 64}
            ],
        )
        scan = _counting_scans(service, monkeypatch)

        assert scan() == uncached | {unresolvable}
        assert scan() == set()
        added = _model(sessions, "added")
        assert scan() == {added}
        assert scan() == set()
        now[0] += PAST_BACKOFF
        assert scan() == {unresolvable}
        assert scan() == set()
    finally:
        service.close()
        engine.dispose()


def test_head_repaired_in_place_is_adopted_into_its_held_set(
    tmp_path, monkeypatch
) -> None:
    """Catches remembering an unresolvable model until the catalog's revision
    set changes: an in-place repair keeps the row, so it would never heal."""
    now = [datetime(2026, 10, 25, tzinfo=UTC)]
    engine, sessions, service = _cache(tmp_path, now)
    catalog = CatalogService(
        sessions, clock=lambda: now[0], cursors=CursorCodec(b"x" * 32)
    )
    try:
        held = _canonical_model(
            publisher="test", slug="held", file_id="weights", file_digest="a" * 64
        ).model_dump(mode="json")
        # The same files under another model: it resolves to the held set.
        alias = _canonical_model(
            publisher="test", slug="alias", file_id="weights", file_digest="a" * 64
        ).model_dump(mode="json")
        alias["source"] = held["source"]
        catalog.import_catalog_models("system:test", [held, alias])
        manifest = service.resolve_artifact_set(
            model_content_sha256=document_sha256(held)
        )
        for spec in _unique_artifacts(manifest.artifacts).values():
            path = service._object_path(spec.sha256)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as handle:
                handle.truncate(spec.expected_bytes)
            service._write_object_receipt(spec, now[0])
        service.adopt_verified_set(manifest)
        alias_digest = document_sha256(alias)
        damaged = {key: value for key, value in alias.items() if key != "files"}
        with engine.begin() as connection:
            connection.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.content_digest == alias_digest)
                .values(document=damaged)
            )

        def bound() -> set[str]:
            with sessions() as session:
                row = session.get(ModelCacheSet, manifest.digest)
                assert row is not None
                stored = service._stored_manifest(row)
                assert stored is not None
                return {item.content_sha256 for item in stored.verified_models}

        scan = _counting_scans(service, monkeypatch)
        assert alias_digest in scan()
        assert alias_digest not in bound()
        catalog.import_catalog_models("system:test", [alias])  # repaired in place
        with sessions() as session:
            assert (
                len(
                    session.scalars(
                        select(CatalogDocumentRevision.id).where(
                            CatalogDocumentRevision.content_digest == alias_digest
                        )
                    ).all()
                )
                == 1
            )
        assert scan() == set()  # still inside its backoff
        now[0] += PAST_BACKOFF
        assert scan() == {alias_digest}
        assert alias_digest in bound()
    finally:
        service.close()
        engine.dispose()
