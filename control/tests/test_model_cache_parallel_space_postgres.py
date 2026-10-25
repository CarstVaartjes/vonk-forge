"""Parallel downloads are admitted only into the NAS space the running ones leave."""

from __future__ import annotations

from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState, ModelCacheCode
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base
from vonk_control.storage_demands import NAS_MODELS, StorageDemands

from .test_model_cache import _artifact

SIZE = 4096
_Usage = namedtuple("_Usage", "total used free")


@pytest.mark.parametrize(("downloads", "fitting"), [(2, 1), (8, 3)])
def test_parallel_downloads_never_overcommit_the_same_free_space(
    postgres_engine, tmp_path: Path, monkeypatch, downloads: int, fitting: int
) -> None:
    """Catches admission that compares each download with the same free space.

    Every download alone fits; together they do not. Comparing each with the
    free disk alone starts all of them and fills the volume between them.
    """
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    free = [fitting * SIZE + SIZE // 2]
    monkeypatch.setattr(
        "vonk_control.model_cache.shutil.disk_usage",
        lambda _: _Usage(10**12, 10**12 - free[0], free[0]),
    )
    service = ModelCacheService(
        sessions, tmp_path / "nas", reserve_bytes=0, fixture_sources=True
    )
    demands = StorageDemands(service._clock)
    service.bind_storage_demands(demands)

    def admit(index: int) -> str:
        model = f"{index + 1:064x}"
        source = tmp_path / f"source-{index}"
        source.mkdir()
        artifact = _artifact(source, bytes([index]) * SIZE, model_content_sha256=model)
        preview = service.download_preview(
            model_content_sha256=model, artifacts=[artifact]
        )
        return service.start_download(
            actor="operator",
            request_key=str(uuid4()),
            plan_digest=str(preview["plan_digest"]),
            model_content_sha256=model,
            artifacts=[artifact],
        ).id

    try:
        with ThreadPoolExecutor(max_workers=downloads) as pool:
            admitted = list(pool.map(admit, range(downloads)))
        claimed = service._claim_operations(
            limit=downloads, respect_backoff=True, transfer_slots=downloads
        )
        assert len(claimed) == fitting
        assert len(claimed) * SIZE <= free[0]
        assert service.storage_summary().in_flight_bytes == fitting * SIZE
        # The rest wait with a typed reason and a retry clock, and ask
        # retention for the space the running downloads leave short.
        waiting = [item for item in admitted if item not in dict(claimed)]
        for operation_id in waiting:
            view = service.get_operation(operation_id)
            assert view.state in {LifecycleState.QUEUED, LifecycleState.BACKOFF}
            assert view.failure is not None
            assert view.failure["code"] == ModelCacheCode.DOWNLOAD_BLOCKED
            assert view.failure["retryable"] is True
            assert view.next_attempt_at is not None
        (demand,) = demands.active()
        assert demand.scope == NAS_MODELS
        assert demand.required_free_bytes == (fitting + 1) * SIZE
        # Re-checking them at once still admits nothing next to the running ones.
        assert (
            service._claim_operations(
                limit=downloads, respect_backoff=False, transfer_slots=downloads
            )
            == []
        )
        # Once the space is there, every waiting download starts by itself.
        free[0] = 10**12
        resumed = service._claim_operations(
            limit=downloads, respect_backoff=False, transfer_slots=downloads
        )
        assert sorted(item for item, _kind in resumed) == sorted(waiting)
    finally:
        service.close()
