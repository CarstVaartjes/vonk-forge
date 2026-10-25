"""The model revision scan costs what changed in the catalog, not its size."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base
from vonk_forge_contracts import document_sha256

from .test_model_cache import _artifact, _canonical_model, _download
from .test_model_removal_reference_lifecycle import _register_model

# Production carries about 300 current model heads, most of them not cached.
UNCACHED_MODELS = 120


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


def test_revision_scan_resolves_only_model_digests_the_catalog_changed(
    tmp_path, monkeypatch
) -> None:
    """Catches a scan that re-resolves every uncached model on every pass: at
    production scale the single worker loop outlives its watchdog and restarts,
    dropping every in-process transfer."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'cache.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = ModelCacheService(
        sessions, tmp_path / "cache", reserve_bytes=0, fixture_sources=True
    )
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

        assert scan() == uncached | {unresolvable}
        assert scan() == set()
        added = _model(sessions, "added")
        assert scan() == {added, unresolvable}
        assert scan() == set()
    finally:
        service.close()
        engine.dispose()
