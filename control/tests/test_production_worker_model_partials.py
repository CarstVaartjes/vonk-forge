"""The production worker collects abandoned model-cache partials."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.agent_jobs import AgentJobService
from vonk_control.jobs import JobService
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.route_runtime import AtomicRouteBundlePublisher
from vonk_control.worker import assemble_production_worker


def test_the_production_worker_tick_removes_an_abandoned_partial(
    tmp_path: Path, request
) -> None:
    """The collector is part of the production worker, not just a class."""

    engine = create_engine(f"sqlite:///{tmp_path / 'worker.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 6, tzinfo=UTC)
    clock = lambda: now
    model_cache = ModelCacheService(sessions, tmp_path / "models", clock=clock)
    request.addfinalizer(model_cache.close)
    worker = assemble_production_worker(
        jobs=JobService(sessions, clock=clock),
        sessions=sessions,
        agent_jobs=AgentJobService(sessions, clock=clock),
        publisher=AtomicRouteBundlePublisher(tmp_path / "routes"),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
        worker_id="control-worker-test",
        artifact_job_root=tmp_path / "artifact-jobs" / "blobs",
        artifact_job_storage_max_bytes=16 * 1024**3,
        artifact_job_retention_seconds=7 * 24 * 60 * 60,
        artifact_job_reconcile_interval_seconds=3600,
        artifact_job_reconcile_batch_limit=1000,
        model_cache=model_cache,
        agent_artifact_root=tmp_path / "agent-artifacts",
        recipe_image_artifact_root=tmp_path / "agent-artifacts",
    )
    request.addfinalizer(worker.close)
    abandoned = model_cache.root / "partials" / ("c" * 64)
    abandoned.mkdir(parents=True)
    (abandoned / f"{'b' * 64}.part").write_bytes(b"unfinished")
    stale = (now - timedelta(days=3)).timestamp()
    for path in (*abandoned.iterdir(), abandoned):
        os.utime(path, (stale, stale))
    worker.run_once()
    assert not abandoned.exists()
