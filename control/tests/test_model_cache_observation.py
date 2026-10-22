"""Receipt/store/preview regressions for resumable cache observation."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from threading import Event
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import RunSwitchCode, WaitReason
from vonk_agent_protocol.agent_words import ProfileDocumentState
from vonk_control import profile_read_observation
from vonk_control.categorized_errors import BookkeepingUnknown
from vonk_control.model_cache import ArtifactSetManifest, ModelCacheService
from vonk_control.model_cache_contract import CacheManifest, ModelCacheEntryStatus
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    ModelCacheSet,
)
from vonk_control.profile_read_observation import profile_read
from vonk_control.run_switch_operations import DatabaseRunSwitchArtifactInspector
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256


@pytest.fixture
def observed_cache(postgres_engine, tmp_path):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    now = [datetime.now(UTC)]
    cache = ModelCacheService(
        sessions,
        tmp_path / "cache",
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now[0],
    )
    # The production GLM recipe selects 142 primary + 4 drafter files. Bytes
    # are small here: availability must size metadata, never hash model data.
    model_raw = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    model_raw["source"] = {
        "repository": "https://huggingface.co/test/observation",
        "revision": "0" * 40,
    }
    model_raw["files"] = []
    for index in range(146):
        data = f"verified-ingress-{index}".encode()
        source = tmp_path / f"file-{index}"
        source.write_bytes(data)
        model_raw["files"].append(
            {
                "id": source.name,
                "path": source.name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "roles": ["weights"],
            }
        )
    model = ModelDefinition.model_validate(model_raw)
    model_digest = document_sha256(model.model_dump(mode="json"))
    recipe_raw = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text()
    )
    selection = recipe_raw["models"][0]
    selection["model"].update(
        publisher=model.identity.publisher,
        slug=model.identity.slug,
        content_sha256=model_digest,
    )
    template = selection["files"][0]
    selection["files"] = [
        {**template, "id": file.id, "file_id": file.id} for file in model.files
    ]
    recipe = RecipeDefinition.model_validate(recipe_raw)
    for definition in (model, recipe):
        document_id, revision_id = str(uuid.uuid4()), str(uuid.uuid4())
        with sessions.begin() as session:
            session.add(
                CatalogDocument(
                    id=document_id,
                    kind=definition.kind,
                    publisher=definition.identity.publisher,
                    slug=definition.identity.slug,
                    title=definition.identity.slug,
                    created_by="test",
                    created_at=now[0],
                    updated_at=now[0],
                )
            )
            session.flush()
            session.add(
                CatalogDocumentRevision(
                    id=revision_id,
                    document_id=document_id,
                    kind=definition.kind,
                    publisher=definition.identity.publisher,
                    slug=definition.identity.slug,
                    revision_number=1,
                    schema_version=2,
                    state=ProfileDocumentState.ACTIVE.value,
                    document=definition.model_dump(mode="json"),
                    content_digest=document_sha256(definition.model_dump(mode="json")),
                    projected={},
                    created_by="test",
                    created_at=now[0],
                )
            )
    manifest = cache.resolve_artifact_set(
        recipe_revision_id=revision_id, model_content_sha256=model_digest
    )
    for spec in manifest.artifacts:
        path = cache._object_path(spec.sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((tmp_path / spec.path).read_bytes())
        cache._write_object_receipt(spec, now[0])
    with cache._session(write=True) as session:
        row = cache._ensure_set(session, manifest)
        row.state = ModelCacheEntryStatus.CACHED
    try:
        yield cache, sessions, manifest, now
    finally:
        cache.close()


def _finish(cache, manifest):
    # Join work submitted by this test only; the request never does this.
    cache._storage_observer._pending[manifest.digest].result(timeout=30)


def _inspect(cache, *, sessions, manifest):
    @profile_read
    def review():
        with sessions() as session:
            revision_id = (
                session.query(CatalogDocumentRevision.id)
                .filter(
                    CatalogDocumentRevision.content_digest
                    == manifest.recipe_revision_sha256
                )
                .scalar()
            )
            return DatabaseRunSwitchArtifactInspector(cache).inspect(
                session,
                model_content_sha256=manifest.model_content_sha256,
                recipe_revision_id=revision_id,
                node_ids=(),
                retention="retain",
                now=cache._clock(),
            )

    return review()


def test_slow_nas_observation_converges_beyond_ten_request_budgets(
    observed_cache, monkeypatch
):
    """Catches nested request deadlines aborting a large set on every retry."""
    cache, _sessions, manifest, _now = observed_cache
    normal_budget = profile_read_observation.PROFILE_READ_STORAGE_SECONDS
    monkeypatch.setattr(profile_read_observation, "PROFILE_READ_STORAGE_SECONDS", 0.1)
    calls = Counter()
    original = cache._read_object_receipt

    def slow(digest, expected):
        calls[digest] += 1
        time.sleep(0.01)  # 146 files take >10 x the 100 ms request budget
        return original(digest, expected)

    monkeypatch.setattr(cache, "_read_object_receipt", slow)
    read = profile_read(
        lambda: cache.download_preview(artifact_set_sha256=manifest.digest)
    )
    started = time.monotonic()
    with pytest.raises(BookkeepingUnknown) as pending:
        read()
    assert pending.value.typed_reason is WaitReason.OBSERVATION_UNAVAILABLE
    assert "objects checked" in str(pending.value)
    assert "next check" in str(pending.value)
    _finish(cache, manifest)
    elapsed = time.monotonic() - started
    assert elapsed > 10 * profile_read_observation.PROFILE_READ_STORAGE_SECONDS
    result = read()
    assert result["already_cached_bytes"] == manifest.expected_bytes
    assert result["new_bytes"] == 0

    def request_must_not_touch_nas(*_args):
        pytest.fail("a ready request repeated NAS observation")

    monkeypatch.setattr(cache._storage_observer, "_read", request_must_not_touch_nas)
    monkeypatch.setattr(cache, "free_bytes", request_must_not_touch_nas)
    # The reduced budget above tests storage convergence. Run the complete
    # SQL/catalog consumer with its real budget, rather than timing unrelated
    # queries against 100 ms under full-suite contention. NAS reads still fail.
    monkeypatch.setattr(
        profile_read_observation, "PROFILE_READ_STORAGE_SECONDS", normal_budget
    )
    inspection = _inspect(cache, sessions=_sessions, manifest=manifest)
    assert inspection.missing_nas_bytes == 0
    assert not inspection.blockers
    assert calls == Counter({spec.sha256: 1 for spec in manifest.artifacts})
    print(
        f"146-object observation: {elapsed:.3f}s vs 0.100s request budget; no restarted objects"
    )


def test_observer_corrects_stale_cached_rows_and_missing_objects(observed_cache):
    """Catches SQL cached flags admitting removed objects or staying pending."""
    cache, sessions, manifest, now = observed_cache
    missing = manifest.artifacts[0]
    cache._object_path(missing.sha256).unlink()  # keep the stale ingress receipt
    cache._storage_observer.tick()  # background discovery, no profile request
    assert cache._storage_observer._discovery is not None
    cache._storage_observer._discovery.result(timeout=30)
    _finish(cache, manifest)
    with sessions() as session:
        row = session.get(ModelCacheSet, manifest.digest)
        assert row.state == ModelCacheEntryStatus.NEEDS_REPAIR
        assert row.verified_bytes == manifest.expected_bytes - missing.expected_bytes
    result = profile_read(
        lambda: cache.download_preview(artifact_set_sha256=manifest.digest)
    )()
    assert result["new_bytes"] == missing.expected_bytes
    assert (
        result["already_cached_bytes"]
        == manifest.expected_bytes - missing.expected_bytes
    )
    cached = profile_read(lambda: cache._managed_cached_objects(manifest))()
    assert missing.sha256 not in cached
    inspection = _inspect(cache, sessions=sessions, manifest=manifest)
    assert inspection.missing_nas_bytes == missing.expected_bytes
    assert any(
        reason.code == RunSwitchCode.NAS_DOWNLOAD_REQUIRED
        for reason in inspection.warnings
    )
    assert not inspection.blockers
    # Restoration after storage loss is discovered without another request.
    path = cache._object_path(missing.sha256)
    path.write_bytes((cache.root.parent / missing.path).read_bytes())
    now[0] += timedelta(seconds=31)
    cache._storage_observer.tick()
    assert cache._storage_observer._discovery is not None
    cache._storage_observer._discovery.result(timeout=30)
    _finish(cache, manifest)
    with sessions() as session:
        assert (
            session.get(ModelCacheSet, manifest.digest).state
            == ModelCacheEntryStatus.CACHED
        )


def _crash_after_checkpoint(database_url, root, manifest_json, clock):
    sessions = sessionmaker(create_engine(database_url), expire_on_commit=False)
    cache = ModelCacheService(
        sessions, root, reserve_bytes=0, fixture_sources=True, clock=lambda: clock
    )
    manifest = ArtifactSetManifest.from_contract(
        CacheManifest.model_validate_json(manifest_json)
    )
    write = cache._storage_observer._write

    def crash(manifest, value):
        write(manifest, value)
        if len(value.objects) == 7:
            os._exit(79)  # real process death after durable publication

    with patch.object(cache._storage_observer, "_write", side_effect=crash):
        cache._storage_observer._observe(manifest)


def test_restart_resumes_checkpoint_without_reobserving_completed_objects(
    observed_cache, monkeypatch
):
    """Catches losing the observed prefix on process/service restart."""
    cache, sessions, manifest, now = observed_cache
    engine = sessions.kw["bind"]
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_after_checkpoint,
        args=(
            engine.url.render_as_string(hide_password=False),
            cache.root,
            manifest.contract().model_dump_json(),
            now[0],
        ),
    )
    process.start()
    try:
        process.join(30)
        assert process.exitcode == 79
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
    checkpoint = cache._storage_observer._read(manifest)
    assert checkpoint.completed_at is None
    assert len(checkpoint.objects) == 7
    calls = set(checkpoint.objects)
    cache.close()
    restarted = ModelCacheService(
        sessions,
        cache.root,
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now[0],
    )
    observed = restarted._stored_object
    resumed = []

    def record(digest, expected):
        resumed.append(digest)
        return observed(digest, expected)

    monkeypatch.setattr(restarted, "_stored_object", record)
    try:
        restarted._storage_observer.tick()
        assert restarted._storage_observer._discovery is not None
        restarted._storage_observer._discovery.result(timeout=30)
        _finish(restarted, manifest)
        assert len(resumed) == 139
        assert not set(calls) & set(resumed)
        result = profile_read(
            lambda: restarted.download_preview(artifact_set_sha256=manifest.digest)
        )()
        assert result["already_cached_bytes"] == manifest.expected_bytes
        assert result["new_bytes"] == 0
    finally:
        restarted.close()


def test_nested_observation_outlives_request_deadline(monkeypatch):
    """Fails on main: nested probes stop at the expired parent request budget."""
    from vonk_control.profile_read_observation import observe_storage

    monkeypatch.setattr(profile_read_observation, "PROFILE_READ_STORAGE_SECONDS", 0.01)
    entered, release, finished = Event(), Event(), Event()
    calls = []

    @observe_storage
    def object_read(index):
        calls.append(index)
        if index == 0:
            entered.set()
            assert release.wait(30)
        return index

    @observe_storage
    def batch():
        try:
            return tuple(object_read(index) for index in range(146))
        finally:
            finished.set()

    read = profile_read(batch)
    try:
        with pytest.raises(BookkeepingUnknown):
            read()
        assert entered.wait(30)
    finally:
        release.set()
    assert finished.wait(30)
    assert calls == list(range(146))
    assert read() == tuple(range(146))


def test_slow_set_does_not_block_unrelated_content(observed_cache, monkeypatch):
    """Catches one stalled set monopolizing all background observation slots."""
    from dataclasses import replace

    cache, _sessions, manifest, _now = observed_cache
    slow = replace(manifest, artifacts=(manifest.artifacts[0],))
    healthy = replace(manifest, artifacts=(manifest.artifacts[1],))
    entered, release = Event(), Event()
    original = cache._stored_object

    def stall(digest, expected):
        if digest == slow.artifacts[0].sha256:
            entered.set()
            assert release.wait(30)
        return original(digest, expected)

    monkeypatch.setattr(cache, "_stored_object", stall)
    cache._storage_observer.schedule(slow)
    try:
        assert entered.wait(30)
        cache._storage_observer.schedule(healthy)
        _finish(cache, healthy)
        observation = cache._storage_observer.read(healthy)
        assert all(entry.available for entry in observation.objects.values())
        assert not cache._storage_observer._pending[slow.digest].done()
    finally:
        release.set()
        _finish(cache, slow)


def test_other_observer_reports_live_checkpoint_without_taking_writer_lock(
    observed_cache, monkeypatch
):
    """Catches API progress staying at zero while the worker owns the scan."""
    cache, sessions, manifest, now = observed_cache
    entered, release = Event(), Event()
    write = cache._storage_observer._write

    def pause(manifest, value):
        write(manifest, value)
        if len(value.objects) == 7:
            entered.set()
            assert release.wait(30)

    monkeypatch.setattr(cache._storage_observer, "_write", pause)
    other = ModelCacheService(
        sessions,
        cache.root,
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now[0],
    )
    cache._storage_observer.schedule(manifest)
    try:
        assert entered.wait(30)
        other._storage_observer.schedule(manifest)
        _finish(other, manifest)
        with pytest.raises(BookkeepingUnknown, match="7/146 objects checked"):
            other._storage_observer.read(manifest)
        assert not cache._storage_observer._pending[manifest.digest].done()
        release.set()
        _finish(cache, manifest)
        # Collect the worker's completed receipt; do not scan its objects again.
        other._storage_observer.schedule(manifest)
        _finish(other, manifest)
        assert other._storage_observer.read(manifest).completed_at is not None
    finally:
        release.set()
        _finish(cache, manifest)
        other.close()


def test_checkpoint_storage_loss_is_visible_and_recovers(observed_cache, monkeypatch):
    """Catches storage failures hidden behind an unexplained zero-progress wait."""
    cache, _sessions, manifest, now = observed_cache
    read = cache._storage_observer._read

    def unavailable(_manifest):
        raise OSError("injected NAS checkpoint outage")

    monkeypatch.setattr(cache._storage_observer, "_read", unavailable)
    cache._storage_observer.schedule(manifest)
    _finish(cache, manifest)
    with pytest.raises(BookkeepingUnknown, match="injected NAS checkpoint outage"):
        cache._storage_observer.read(manifest)
    monkeypatch.setattr(cache._storage_observer, "_read", read)
    now[0] += timedelta(seconds=6)
    cache._storage_observer.schedule(manifest)
    _finish(cache, manifest)
    observation = cache._storage_observer.read(manifest)
    assert observation.completed_at is not None
    assert all(entry.available for entry in observation.objects.values())


def test_model_only_checkpoint_reloads_required_null_fields(
    observed_cache, monkeypatch
):
    """Catches a writer dropping required nulls so another process starts over."""
    from dataclasses import replace

    cache, sessions, manifest, now = observed_cache
    model_only = replace(
        manifest, recipe_revision_sha256=None, model_definition_ref=None
    )
    cache._storage_observer.schedule(model_only)
    _finish(cache, model_only)
    other = ModelCacheService(
        sessions,
        cache.root,
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now[0],
    )

    def must_reuse_checkpoint(*_args):
        pytest.fail("another observer discarded a completed model-only checkpoint")

    monkeypatch.setattr(other, "_stored_object", must_reuse_checkpoint)
    try:
        other._storage_observer.schedule(model_only)
        _finish(other, model_only)
        result = other._storage_observer.read(model_only)
        assert result.completed_at is not None
        assert result.manifest.recipe_revision_sha256 is None
        assert result.manifest.model_definition_ref is None
    finally:
        other.close()


def test_refresh_preserves_measured_facts_for_any_read_interval(
    observed_cache, monkeypatch
):
    """Catches each late retry landing in an expiry gap and waiting forever."""
    cache, _sessions, manifest, now = observed_cache
    cache._storage_observer.schedule(manifest)
    _finish(cache, manifest)
    completed_at = cache._storage_observer.read(manifest).completed_at
    now[0] += timedelta(seconds=31)
    entered, release = Event(), Event()
    original = cache._stored_object

    def slow(digest, expected):
        entered.set()
        assert release.wait(30)
        return original(digest, expected)

    monkeypatch.setattr(cache, "_stored_object", slow)
    cache._storage_observer.schedule(manifest)
    try:
        assert entered.wait(30)
        # A late read reuses known facts while the current refresh progresses.
        observation = cache._storage_observer.read(manifest)
        assert observation.completed_at == completed_at
        assert all(entry.available for entry in observation.objects.values())
        assert not cache._storage_observer._pending[manifest.digest].done()
    finally:
        release.set()
        _finish(cache, manifest)
    assert cache._storage_observer.read(manifest).completed_at == now[0]


def test_capacity_observation_recovers_after_all_members_are_checkpointed(
    observed_cache, monkeypatch
):
    """Catches a recovered capacity read leaving the old error as a permanent wait."""
    cache, _sessions, manifest, now = observed_cache
    capacity = cache.free_bytes

    def unavailable():
        raise OSError("injected capacity observation outage")

    monkeypatch.setattr(cache, "free_bytes", unavailable)
    cache._storage_observer.schedule(manifest)
    _finish(cache, manifest)
    with pytest.raises(BookkeepingUnknown, match="capacity observation outage"):
        cache._storage_observer.read(manifest)

    def members_are_checkpointed(*_args):
        pytest.fail("capacity recovery restarted completed member observations")

    monkeypatch.setattr(cache, "_stored_object", members_are_checkpointed)
    monkeypatch.setattr(cache, "free_bytes", capacity)
    now[0] += timedelta(seconds=6)
    cache._storage_observer.schedule(manifest)
    _finish(cache, manifest)
    assert cache._storage_observer.read(manifest).completed_at is not None


def test_cold_api_preparation_collects_worker_checkpoint_on_first_review(
    observed_cache, monkeypatch
):
    """Catches a prepared model's first profile review being falsely refused."""
    cache, sessions, manifest, now = observed_cache
    cache._storage_observer.schedule(manifest)
    _finish(cache, manifest)
    api = ModelCacheService(
        sessions,
        cache.root,
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now[0],
    )

    def must_collect_published_facts(*_args):
        pytest.fail(
            "cold API reader rescanned a prepared model instead of collecting it"
        )

    monkeypatch.setattr(api, "_stored_object", must_collect_published_facts)
    try:
        inspection = _inspect(api, sessions=sessions, manifest=manifest)
        assert inspection.artifact_set_sha256 == manifest.digest
        assert inspection.missing_nas_bytes == 0
        assert not inspection.blockers
    finally:
        api.close()


def test_cold_checkpoint_collection_outlives_a_timed_out_reader(
    observed_cache, monkeypatch
):
    """Catches an unbounded cold read or restarting its collection on every retry."""
    cache, sessions, manifest, now = observed_cache
    cache._storage_observer.schedule(manifest)
    _finish(cache, manifest)
    api = ModelCacheService(
        sessions,
        cache.root,
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now[0],
    )
    entered, release = Event(), Event()
    calls = []
    original = api._storage_observer._read

    def slow(manifest):
        calls.append(manifest.digest)
        entered.set()
        assert release.wait(30)
        return original(manifest)

    monkeypatch.setattr(api._storage_observer, "_read", slow)
    monkeypatch.setattr(profile_read_observation, "PROFILE_READ_STORAGE_SECONDS", 0.02)
    api._storage_observer.schedule(manifest)
    try:
        assert entered.wait(30)
        with pytest.raises(BookkeepingUnknown):
            profile_read(api._storage_observer.read)(manifest)
        assert not release.is_set()
        release.set()
        _finish(api, manifest)
        assert (
            profile_read(api._storage_observer.read)(manifest).completed_at is not None
        )
        assert calls == [manifest.digest]
    finally:
        release.set()
        api.close()
