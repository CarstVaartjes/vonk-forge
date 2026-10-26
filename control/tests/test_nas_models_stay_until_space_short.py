"""NAS model files stay until space is short; then only the LRU collector evicts."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState, RunState
from vonk_control.models import RecipeRun
from vonk_control.storage_demands import StorageDemands
from vonk_forge_contracts import document_sha256

from .test_catalog_revision_collection import Catalog
from .test_model_cache import _artifact, _canonical_model, _download
from .test_model_cache_revisions import _contains, _revision
from .test_model_removal_decided_by_evidence import _installation
from .test_model_removal_reference_lifecycle import _register_model, _removal_service
from .test_unused_storage_collection import TOTAL, _models, _settle

TICKS = 5
ALL = {"a": True, "n": True, "l": True}
DIGESTS = {name: hashlib.sha256(name.encode()).hexdigest() for name in ("old", "new")}
SHARED = hashlib.sha256(b"shared").hexdigest()


def _tick(service) -> None:
    service._revision_scan_at = None
    service.reconcile_model_revisions()
    service.run_pending()
    _settle(service)


def _shelf(postgres_engine, tmp_path, *, installed: bool):
    """A (a superseded revision) and N (its successor) share a tokenizer file; L
    is another model, loaded by a running installation. With ``installed`` the
    successor's plan names its real file digests, as a real compiled plan does."""
    service, sessions = _removal_service(postgres_engine, tmp_path)
    world = Catalog(postgres_engine)
    _, a, _ = _revision(service, sessions, tmp_path / "a", data=b"old")
    n_model, n, _ = _revision(
        service, sessions, tmp_path / "n", data=b"new", note="new"
    )
    _, loaded, _ = _revision(
        service, sessions, tmp_path / "l", slug="loaded", data=b"lll"
    )
    if installed:
        _installation(world, n, file_digests=(DIGESTS["new"], SHARED))
    installation = _installation(world, loaded)
    with sessions.begin() as session:
        run = session.scalar(
            select(RecipeRun).where(RecipeRun.installation_id == installation)
        )
        assert run is not None
        run.state = RunState.RUNNING
    return service, sessions, world, {"a": a, "n": n, "l": loaded}, n_model


def _alive(sessions, sets) -> dict[str, bool]:
    return {name: _contains(sessions, digest) for name, digest in sets.items()}


def _sources_gone(root: Path, data: bytes, model: str) -> list[dict[str, object]]:
    """The artifacts of a request whose every source file is gone."""
    root.mkdir()
    artifacts = [
        _artifact(root, data, model_content_sha256=model),
        _artifact(
            root,
            b"shared",
            artifact_id="tokenizer",
            path="tokenizer.json",
            model_content_sha256=model,
        ),
    ]
    for source in root.iterdir():
        source.unlink()
    return artifacts


def test_cached_sets_survive_every_tick_while_space_is_ample(
    postgres_engine, tmp_path
) -> None:
    """Catches the 30-second revision scan releasing a model nobody asked to
    free: with ample space files are only ever evicted by need."""
    service, sessions, world, sets, _ = _shelf(
        postgres_engine, tmp_path, installed=False
    )
    try:
        for _ in range(TICKS):
            _tick(service)
        assert _alive(sessions, sets) == ALL
        assert _models(world, service, free=TOTAL // 2).collect().models == 0
        assert _alive(sessions, sets) == ALL
    finally:
        service.close()


def test_pressure_evicts_the_superseded_revision_but_keeps_what_the_successor_shares(
    postgres_engine, tmp_path
) -> None:
    """Catches a superseded revision that is never evicted because it shares a
    file (tokenizer, config) with an installed successor, and eviction that
    deletes the shared bytes or counts them as freed."""
    service, sessions, world, sets, n_model = _shelf(
        postgres_engine, tmp_path, installed=True
    )
    try:
        for _ in range(TICKS):
            _tick(service)
        assert _alive(sessions, sets) == ALL

        short = _models(world, service).collect()
        assert short.models == 1, short.kept
        _settle(service)
        assert _alive(sessions, sets) == {"a": False, "n": True, "l": True}
        assert not service._object_path(DIGESTS["old"]).exists()
        assert service._object_path(SHARED).read_bytes() == b"shared"
        assert service._object_path(DIGESTS["new"]).read_bytes() == b"new"

        # Every source is gone: the installed successor is served from the
        # cache or not at all, never by another transfer.
        served = _download(
            service,
            _sources_gone(tmp_path / "gone", b"new", n_model),
            model_content_sha256=n_model,
            request_key=str(uuid.uuid4()),
        )
        assert served.state == LifecycleState.SUCCEEDED
    finally:
        service.close()


def test_a_refused_download_is_admitted_after_the_collector_made_room(
    postgres_engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a refused NAS download that needs an operator: the demand makes
    the collector evict the superseded revision, and the same request is then
    admitted, moving only the bytes it did not have."""
    service, sessions, world, sets, _ = _shelf(
        postgres_engine, tmp_path, installed=True
    )
    try:
        size = 3  # the canonical model's one file
        free = [size - 2]  # two bytes short; the superseded revision frees three
        monkeypatch.setattr(service, "free_bytes", lambda: free[0])
        demands = StorageDemands(lambda: world.now)
        service.bind_storage_demands(demands)
        # The collector's clock decides when a demand expires.
        collector = _models(
            world,
            service,
            free=TOTAL // 2,
            demands=demands,
            disk_usage=lambda _path: (1000, free[0]),
            low_free_fraction=0.0,
            low_free_cap_fraction=0.0,
            reserve_fraction=0.0,
            reserve_floor_bytes=0,
        )
        data = b"c" * size
        later = _canonical_model(
            publisher="test",
            slug="later",
            file_id="weights",
            file_digest=hashlib.sha256(data).hexdigest(),
        )
        _register_model(sessions, later)
        digest = document_sha256(later.model_dump(mode="json"))
        (tmp_path / "c").mkdir()
        request_key = str(uuid.uuid4())
        artifacts = [_artifact(tmp_path / "c", data, model_content_sha256=digest)]
        waiting = _download(
            service,
            artifacts,
            model_content_sha256=digest,
            request_key=request_key,
        )
        assert waiting.state != LifecycleState.SUCCEEDED
        assert _alive(sessions, sets)["a"]

        # The refusal asked for room: the collector evicts the superseded
        # revision (and nothing the successor or the loaded model uses).
        freed = collector.collect()
        assert freed.models == 1, (freed.kept, freed.scopes)
        _settle(service)
        assert _alive(sessions, sets) == {"a": False, "n": True, "l": True}
        free[0] = size + 1

        # The same request, with no operator action, now completes.
        due = waiting.next_attempt_at
        assert due is not None
        resume = datetime.fromisoformat(due) + timedelta(seconds=1)
        service._clock = lambda: resume
        service.run_pending()
        done = service.get_operation(waiting.id)
        assert done.state == LifecycleState.SUCCEEDED, done
    finally:
        service.close()
