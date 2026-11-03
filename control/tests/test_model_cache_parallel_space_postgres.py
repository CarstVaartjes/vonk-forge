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
from vonk_control.model_cache.persistence import _store_operation_payload
from vonk_control.models import Base, ModelCacheOperation
from vonk_control.storage_demands import NAS_MODELS, StorageDemands

from .test_model_cache import _artifact, _download

SIZE = 4096
_Usage = namedtuple("_Usage", "total used free")


def _service(postgres_engine, tmp_path: Path, monkeypatch, free: list[int]):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    monkeypatch.setattr(
        "vonk_control.model_cache.shutil.disk_usage",
        lambda _: _Usage(10**12, 10**12 - free[0], free[0]),
    )
    service = ModelCacheService(
        sessions, tmp_path / "nas", free_fraction=0, fixture_sources=True
    )
    return service, sessions


def _admit(service: ModelCacheService, root: Path, model: str, data: bytes) -> str:
    root.mkdir()
    artifact = _artifact(root, data, model_content_sha256=model)
    preview = service.download_preview(model_content_sha256=model, artifacts=[artifact])
    return service.start_download(
        actor="operator",
        request_key=str(uuid4()),
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=model,
        artifacts=[artifact],
    ).id


@pytest.mark.parametrize(("downloads", "fitting"), [(2, 1), (8, 3)])
def test_parallel_downloads_never_overcommit_the_same_free_space(
    postgres_engine, tmp_path: Path, monkeypatch, downloads: int, fitting: int
) -> None:
    """Catches admission that compares each download with the same free space.

    Every download alone fits; together they do not. Comparing each with the
    free disk alone starts all of them and fills the volume between them.
    """
    free = [fitting * SIZE + SIZE // 2]
    service, sessions = _service(postgres_engine, tmp_path, monkeypatch, free)
    demands = StorageDemands(service._clock)
    service.bind_storage_demands(demands)

    def admit(index: int) -> str:
        return _admit(
            service,
            tmp_path / f"source-{index}",
            f"{index + 1:064x}",
            bytes([index]) * SIZE,
        )

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
        # The running downloads own these waits: their windows restart instead
        # of running out behind them.
        with sessions() as session:
            for operation_id in waiting:
                row = session.get(ModelCacheOperation, operation_id)
                assert row is not None and row.progress_advanced_at is not None
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


@pytest.mark.parametrize("partial_kept", [False, True])
def test_received_bytes_count_only_while_their_partial_is_on_disk(
    postgres_engine, tmp_path: Path, monkeypatch, partial_kept: bool
) -> None:
    """Catches a ledger read as landed bytes after its partial was discarded.

    A digest mismatch deletes the partial but leaves the ledger's received
    bytes: the retry still owes the whole object. A partial that is still on
    disk is a resume that owes only the rest.
    """
    free = [SIZE // 2]
    service, sessions = _service(postgres_engine, tmp_path, monkeypatch, free)
    try:
        operation_id = _admit(service, tmp_path / "source", "1" * 64, b"w" * SIZE)
        kept = SIZE * 3 // 4
        with sessions.begin() as session:
            row = session.get(ModelCacheOperation, operation_id)
            assert row is not None
            payload = service._transfer_or_none(row)
            assert payload is not None
            ((digest, entry),) = payload.transfer.artifacts.items()
            ledger = payload.transfer.model_copy(
                update={
                    "artifacts": {
                        digest: entry.model_copy(update={"received_bytes": kept})
                    }
                }
            )
            _store_operation_payload(
                row, row.kind, payload.model_copy(update={"transfer": ledger})
            )
            set_digest = row.artifact_set_sha256
        assert set_digest is not None
        if partial_kept:
            partial = service._partial_path(set_digest, digest)
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"w" * kept)
        claimed = service._claim_operations(
            limit=1, respect_backoff=False, transfer_slots=1
        )
        assert [item for item, _kind in claimed] == (
            [operation_id] if partial_kept else []
        )
    finally:
        service.close()


def test_an_object_two_downloads_share_is_reserved_once(
    postgres_engine, tmp_path: Path, monkeypatch
) -> None:
    """Catches charging one object twice: two models naming the same file."""
    free = [SIZE + SIZE // 2]
    service, _sessions = _service(postgres_engine, tmp_path, monkeypatch, free)
    try:
        admitted = [
            _admit(service, tmp_path / f"source-{index}", model, b"s" * SIZE)
            for index, model in enumerate(("1" * 64, "2" * 64))
        ]
        claimed = service._claim_operations(
            limit=2, respect_backoff=True, transfer_slots=2
        )
        assert sorted(item for item, _kind in claimed) == sorted(admitted)
        assert service.storage_summary().in_flight_bytes == SIZE
    finally:
        service.close()


def test_a_running_repair_holds_its_copy_beside_the_verified_one(
    postgres_engine, tmp_path: Path, monkeypatch
) -> None:
    """Catches counting a running repair as needing nothing.

    A repair rewrites a stored object next to its verified copy. A sibling
    download fits beside the repair's remaining bytes (none) but not beside
    that copy, so it waits until the repair ends.
    """
    free = [10**9]
    service, sessions = _service(postgres_engine, tmp_path, monkeypatch, free)
    try:
        source = tmp_path / "repaired"
        source.mkdir()
        stored = _download(
            service,
            [_artifact(source, b"r" * SIZE, model_content_sha256="1" * 64)],
            model_content_sha256="1" * 64,
            request_key=str(uuid4()),
        )
        assert stored.state == LifecycleState.SUCCEEDED, stored.last_error
        set_digest = stored.artifact_set_sha256
        assert set_digest is not None
        free[0] = SIZE + SIZE // 2
        repair = service.start_repair(
            actor="operator",
            request_key=str(uuid4()),
            artifact_set_sha256=set_digest,
            plan_digest=str(service.repair_preview(set_digest)["plan_digest"]),
        )
        assert service._claim_operations(
            limit=1, respect_backoff=False, transfer_slots=1
        ) == [(repair.id, "repair")]
        sibling = _admit(service, tmp_path / "sibling", "2" * 64, b"s" * SIZE)
        assert (
            service._claim_operations(limit=1, respect_backoff=False, transfer_slots=1)
            == []
        )
        with sessions.begin() as session:
            row = session.get(ModelCacheOperation, repair.id)
            assert row is not None
            row.state = LifecycleState.SUCCEEDED
        assert service._claim_operations(
            limit=1, respect_backoff=False, transfer_slots=1
        ) == [(sibling, "download")]
    finally:
        service.close()
