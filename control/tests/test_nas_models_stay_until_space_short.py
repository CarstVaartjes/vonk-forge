"""NAS model files stay until space is short; then only the LRU collector evicts."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from vonk_control.models import RecipeRun

from .test_catalog_revision_collection import Catalog
from .test_model_cache import _artifact, _download
from .test_model_cache_revisions import _contains, _revision
from .test_model_removal_decided_by_evidence import _installation
from .test_model_removal_reference_lifecycle import _removal_service
from .test_unused_storage_collection import (
    TOTAL,
    _models,
    _settle,
)

TICKS = 5


def _tick(service) -> None:
    service._revision_scan_at = None
    service.reconcile_model_revisions()
    service.run_pending()
    _settle(service)


def _shelf(postgres_engine, tmp_path):
    """Set A (a superseded revision, unreferenced), B (its successor, used by an
    installation) and
    L (another model, loaded by a running installation)."""
    service, sessions = _removal_service(postgres_engine, tmp_path)
    world = Catalog(postgres_engine)
    _, a, _ = _revision(service, sessions, tmp_path / "a", slug="idle", data=b"old")
    b_model, b, _ = _revision(
        service, sessions, tmp_path / "b", data=b"new", note="new"
    )
    _, loaded, _ = _revision(
        service, sessions, tmp_path / "l", slug="loaded", data=b"lll"
    )
    _installation(world, b)
    installation = _installation(world, loaded)
    with sessions.begin() as session:
        run = session.scalar(
            select(RecipeRun).where(RecipeRun.installation_id == installation)
        )
        assert run is not None
        run.state = "running"
    return service, sessions, world, {"a": a, "b": b, "l": loaded}, {"b": b_model}


def _alive(sessions, sets) -> dict[str, bool]:
    return {name: _contains(sessions, digest) for name, digest in sets.items()}


def test_cached_sets_survive_every_tick_while_space_is_ample(
    postgres_engine, tmp_path
) -> None:
    """Catches the 30-second revision scan releasing a model that
    nobody asked to free: with ample space files are only ever evicted by need."""
    service, sessions, world, sets, _ = _shelf(postgres_engine, tmp_path)
    try:
        for _ in range(TICKS):
            _tick(service)
        assert _alive(sessions, sets) == {"a": True, "b": True, "l": True}
        ample = _models(world, service, free=TOTAL // 2).collect()
        assert ample.models == 0
        assert _alive(sessions, sets) == {"a": True, "b": True, "l": True}
    finally:
        service.close()


def test_pressure_evicts_only_the_unreferenced_set_and_b_is_served_without_transfer(
    postgres_engine, tmp_path
) -> None:
    """Catches removal that ignores space, or that removes a current or loaded
    set when room is short; a kept set must be served without a new transfer."""
    service, sessions, world, sets, models = _shelf(postgres_engine, tmp_path)
    try:
        for _ in range(TICKS):
            _tick(service)
        assert _alive(sessions, sets) == {"a": True, "b": True, "l": True}

        short = _models(world, service).collect()
        assert short.models == 1, short.kept
        _settle(service)
        assert _alive(sessions, sets) == {"a": False, "b": True, "l": True}

        # Every source is gone: a request for B is served from the cache or
        # not at all, never by another transfer.
        gone = tmp_path / "gone"
        gone.mkdir()
        artifacts = [
            _artifact(gone, b"new", model_content_sha256=models["b"]),
            _artifact(
                gone,
                b"shared",
                artifact_id="tokenizer",
                path="tokenizer.json",
                model_content_sha256=models["b"],
            ),
        ]
        for source in gone.iterdir():
            source.unlink()
        served = _download(
            service,
            artifacts,
            model_content_sha256=models["b"],
            request_key=str(uuid.uuid4()),
        )
        assert served.state == "succeeded"
    finally:
        service.close()
