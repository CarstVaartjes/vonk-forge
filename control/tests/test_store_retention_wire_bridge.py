"""A PostgreSQL load's demand drives native eviction and clears its disk gate."""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from vonk_agent_protocol import AgentOperation, InventoryVerdict, canonical_message
from vonk_control.inventory_repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache_contract import CacheManifest
from vonk_control.models import Base, ModelCacheSet
from vonk_control.spark_store_retention import store_retention_request

from .test_catalog_revision_collection import NODE, Catalog
from .test_model_cache import _artifact, _download
from .test_spark_store_retention import _waiting_load
from .test_unused_storage_collection import (
    A_MODEL,
    _collector,
    _head_recipe_naming,
    _profile,
    _report_disk,
)
from .wire_probes import prebuilt_probe


@pytest.fixture(scope="session")
def store_retention_probe() -> Path:
    return prebuilt_probe("VONK_STORE_RETENTION_PROBE")


def test_full_disk_load_evicts_unused_objects_keeps_profile_content_and_retries(
    tmp_path: Path, postgres_engine, store_retention_probe: Path
) -> None:
    """Catches native low-water-only collection: free space is above that line,
    yet below the accepted load's demand, which must reach the agent's LRU.
    """
    Base.metadata.create_all(postgres_engine)
    world = Catalog(postgres_engine)
    now = datetime.now(UTC)
    world.now = now
    target = 120 * 1024
    cache = ModelCacheService(
        world.sessions, tmp_path / "nas-cache", reserve_bytes=0, fixture_sources=True
    )
    try:
        downloaded = _download(
            cache,
            [_artifact(tmp_path, b"weights", model_content_sha256=A_MODEL)],
            model_content_sha256=A_MODEL,
            request_key=str(uuid.uuid4()),
        )
        assert downloaded.artifact_set_sha256 is not None
        with world.sessions() as session:
            cached = session.get(ModelCacheSet, downloaded.artifact_set_sha256)
            assert cached is not None
            manifest = CacheManifest.model_validate_json(json.dumps(cached.manifest))
        _head_recipe_naming(world, A_MODEL)
        _profile(world, "vonk-forge/glm")
        application_id = _waiting_load(world, required=target)
        collector = _collector(world, now=now, free=90 * 1024)
        _report_disk(world.sessions, 90 * 1024, at=now, total=512 * 1024)
        with world.sessions() as session:
            request = store_retention_request(session, NODE, now)
        assert request is not None and request.required_free_bytes == target
        root = tmp_path / "spark"
        objects = root / "distribution/models"
        objects.mkdir(parents=True)
        pinned = objects / manifest.artifacts[0].sha256
        pinned.write_bytes(b"weights")
        oldest, newest = objects / ("e" * 64), objects / ("f" * 64)
        oldest.write_bytes(bytes(64 * 1024))
        time.sleep(0.02)
        newest.write_bytes(bytes(64 * 1024))
        capacity = 90 * 1024 + sum(path.stat().st_size for path in objects.iterdir())
        verdict = tmp_path / "verdict.json"
        verdict.write_text(
            InventoryVerdict(
                untracked_installations=[], store_retention=request
            ).model_dump_json(exclude_none=True)
        )
        result = subprocess.run(
            [str(store_retention_probe), str(root), str(verdict), str(capacity)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert not oldest.exists(), result.stderr
        assert newest.exists() and pinned.read_bytes() == b"weights"
        measured_free = int(result.stdout)
        assert measured_free >= target
        # The collector's real measured result returns through inventory storage,
        # then the same waiting application can pass its ordinary disk retry.
        InventoryRepository(
            world.sessions, clock=lambda: now + timedelta(seconds=1)
        ).record(
            NodeInventoryReport.model_validate_json(
                canonical_message(
                    {
                        "node_id": NODE,
                        "observed_at": now + timedelta(seconds=1),
                        "disk_total_bytes": capacity,
                        "disk_free_bytes": measured_free,
                        "host_memory_total_bytes": 1000,
                        "host_memory_free_bytes": 1000,
                        "gpu_memory_total_bytes": 1000,
                        "gpu_memory_free_bytes": 1000,
                        "gpu_count": 1,
                        "memory_pool": "shared",
                        "artifact_store_read_only": False,
                        "capabilities": (),
                        "nvidia_driver_version": "580.173.02",
                        "container_runtime_version": "29.6.2",
                        "schema_version": 1,
                    }
                )
            )
        )
        world.now = now + timedelta(seconds=2)
        assert (
            collector.relief_for_spark(
                NODE,
                target,
                source="profile-load",
                subject=application_id,
                reason=AgentOperation.RECIPE_INSTALL,
            )
            is None
        )

    finally:
        cache.close()
