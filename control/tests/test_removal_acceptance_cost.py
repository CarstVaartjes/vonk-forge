"""Removal acceptance and the NAS storage collector cost only what they touch."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import event, insert, select
from sqlalchemy.engine import Engine
from vonk_control.model_cache.artifacts import ArtifactSetManifest
from vonk_control.models import ModelCacheSet, ModelCacheSetArtifact
from vonk_control.storage_demands import NAS_MODELS, StorageDemands
from vonk_control.unused_storage_collection import UnusedStorageCollector

from .test_model_cache import _artifact, _download
from .test_model_removal_reference_lifecycle import _removal_service

UNRELATED_SETS = 60
FILES_PER_SET = 10


def _digest(*parts: object) -> str:
    return hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()


def _seed_unrelated_sets(engine: Engine, template) -> None:
    now = datetime.now(UTC)
    sets, members = [], []
    for index in range(UNRELATED_SETS):
        model = _digest("model", index)
        specs = tuple(
            replace(
                template,
                key=f"f{j}",
                artifact_id=f"f{j}",
                path=f"f{j}.bin",
                sha256=_digest("set", index, j),
                expected_bytes=1_000,
                model_content_sha256=model,
            )
            for j in range(FILES_PER_SET)
        )
        manifest = ArtifactSetManifest(
            model_content_sha256=model,
            recipe_revision_sha256=None,
            model_content_digests=(model,),
            artifacts=specs,
        )
        sets.append(
            {
                "artifact_set_sha256": manifest.digest,
                "schema_version": 2,
                "model_content_sha256": model,
                "recipe_revision_sha256": None,
                "manifest": manifest.document(),
                "expected_bytes": manifest.expected_bytes,
                "verified_bytes": manifest.expected_bytes,
                "state": "cached",
                "protected": False,
                "protected_reasons": [],
                "created_at": now,
                "updated_at": now,
                "verified_at": now,
                "last_accessed_at": now,
                "last_error": None,
            }
        )
        members += [
            {
                "artifact_set_sha256": manifest.digest,
                "artifact_key": spec.key,
                "artifact_sha256": spec.sha256,
                "path": spec.path,
            }
            for spec in specs
        ]
    with engine.begin() as connection:
        connection.execute(insert(ModelCacheSet), sets)
        connection.execute(insert(ModelCacheSetArtifact), members)


def test_accepting_a_removal_does_not_read_other_sets_memberships(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    """Catches acceptance selecting every membership row of every other set."""

    service, sessions = _removal_service(postgres_engine, tmp_path)
    try:
        root = tmp_path / "src"
        root.mkdir()
        model = "e" * 64
        artifacts = [_artifact(root, b"template", model_content_sha256=model)]
        op = _download(
            service,
            artifacts,
            model_content_sha256=model,
            request_key="00000000-0000-4000-8000-000000000f02",
        )
        assert op.state == "succeeded", op.last_error
        set_digest = op.artifact_set_sha256
        assert set_digest is not None
        with sessions() as session:
            row = session.get(ModelCacheSet, set_digest)
            assert row is not None
            stored = service._stored_manifest(row)
            assert stored is not None
            template = stored.artifacts[0]
        _seed_unrelated_sets(postgres_engine, template)

        largest = 0

        def watch(conn, cursor, statement, parameters, context, executemany):
            nonlocal largest
            largest = max(largest, cursor.rowcount or 0)

        event.listen(postgres_engine, "after_cursor_execute", watch)
        try:
            service.accept_unused_set_removal(
                set_digest,
                actor="test",
                request_key="00000000-0000-4000-8000-000000000f03",
                verify=lambda session, sets: None,
            )
        finally:
            event.remove(postgres_engine, "after_cursor_execute", watch)
        with sessions() as session:
            own = len(
                session.scalars(
                    select(ModelCacheSetArtifact).where(
                        ModelCacheSetArtifact.artifact_set_sha256 == set_digest
                    )
                ).all()
            )
        assert 0 < own < UNRELATED_SETS * FILES_PER_SET
        assert largest <= own, (largest, own)
    finally:
        service.close()


def _collector(clock: list[datetime], demands: StorageDemands):
    collector = UnusedStorageCollector(
        None,  # type: ignore[arg-type]
        clock=lambda: clock[0],
        lifecycle=None,  # type: ignore[arg-type]
        demands=demands,
        scan_interval=timedelta(seconds=30),
    )
    collects: list[datetime] = []

    def collect() -> SimpleNamespace:
        collects.append(clock[0])
        return SimpleNamespace(installations=0, images=0, models=0)

    collector.collect = collect  # type: ignore[method-assign]
    return collector, collects


def test_a_new_nas_demand_makes_the_collector_due_now() -> None:
    """Catches a refused NAS download waiting out the scan interval."""

    clock = [datetime(2026, 11, 2, tzinfo=UTC)]
    demands = StorageDemands(lambda: clock[0])
    collector, collects = _collector(clock, demands)
    collector.tick()
    clock[0] += timedelta(seconds=1)
    demands.request("spark:node-1", 10, source="load", reason="x")
    collector.tick()
    assert len(collects) == 1
    demands.request(NAS_MODELS, 10, source="model-download", reason="x")
    collector.tick()
    assert len(collects) == 2


def test_a_demand_refiled_every_few_seconds_collects_once_per_scan() -> None:
    """Catches a blocked download refiling its demand every 5 s and so spinning
    the collector at that rate instead of the scan interval."""

    clock = [datetime(2026, 11, 2, tzinfo=UTC)]
    demands = StorageDemands(lambda: clock[0])
    collector, collects = _collector(clock, demands)
    collector.tick()
    for _ in range(5):  # 25 s of refiles, inside one scan interval
        clock[0] += timedelta(seconds=5)
        demands.request(NAS_MODELS, 10, source="model-download", reason="x")
        collector.tick()
    assert len(collects) == 2  # the first scan and the one the new demand woke
