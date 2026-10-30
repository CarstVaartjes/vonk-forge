"""Unfinished model downloads are collected a day after nothing writes them."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import LifecycleState
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache.partials_collection import ModelPartialsCollector
from vonk_control.models import ModelCacheOperation

from .test_model_cache import _artifact, _download, cache

__all__ = ["cache"]

_HALF_MEBIBYTE = 512 * 1024
_NOW = datetime.now(UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = _NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


@pytest.fixture
def collector(
    cache: tuple[ModelCacheService, sessionmaker[Session]],
) -> Iterator[tuple[ModelPartialsCollector, ModelCacheService, _Clock]]:
    service, sessions = cache
    clock = _Clock()
    yield (
        ModelPartialsCollector(sessions, service, service.root, clock=clock),
        service,
        clock,
    )
    service.close()


def _orphan(service: ModelCacheService, name: str) -> Path:
    directory = service.root / "partials" / name
    directory.mkdir(parents=True)
    (directory / f"{'b' * 64}.part").write_bytes(b"unfinished")
    stale = _NOW.timestamp() - 3 * 86400
    for path in (*directory.iterdir(), directory):
        os.utime(path, (stale, stale))
    return directory


def _interrupted(
    service: ModelCacheService, tmp_path: Path, *, repair: bool
) -> tuple[str, Path]:
    artifact = _artifact(tmp_path, b"x" * (2 * 1024 * 1024 + 3))
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key=str(uuid4()),
        interrupt_after_bytes=None if repair else _HALF_MEBIBYTE,
    )
    if not repair:
        service._run_download(
            downloaded.id, force=False, interrupt_after_bytes=_HALF_MEBIBYTE
        )
        operation_id = downloaded.id
    else:
        digest = downloaded.artifact_set_sha256
        assert digest is not None
        preview = service.repair_preview(digest)
        started = service.start_repair(
            actor="test",
            request_key=str(uuid4()),
            artifact_set_sha256=digest,
            plan_digest=str(preview["plan_digest"]),
        )
        service._run_download(
            started.id, force=True, interrupt_after_bytes=_HALF_MEBIBYTE
        )
        operation_id = started.id
    assert service.get_operation(operation_id).state == LifecycleState.BACKOFF
    partials = service.root / "partials"
    for leftover in partials.iterdir():
        if not any(leftover.iterdir()):
            leftover.rmdir()
    (directory,) = partials.iterdir()
    return operation_id, directory


def _mark_ended(sessions: sessionmaker[Session], operation_id: str) -> None:
    with sessions.begin() as session:
        row = session.get(ModelCacheOperation, operation_id)
        assert row is not None
        row.state = LifecycleState.CANCELLED.value


def test_orphan_directory_without_a_set_row_goes_after_a_day(collector) -> None:
    gatherer, service, clock = collector
    directory = _orphan(service, "c" * 64)
    clock.advance(hours=-72 + 1)
    assert gatherer.collect() == (0, False)
    assert directory.exists()
    clock.advance(hours=72)
    assert gatherer.collect() == (1, False)
    assert not directory.exists()
    assert gatherer.collect() == (0, False)


def test_dead_repair_directory_goes_after_a_day(collector) -> None:
    gatherer, service, clock = collector
    directory = _orphan(service, f"repair-{uuid4()}")
    clock.advance(hours=24)
    assert gatherer.collect() == (1, False)
    assert not directory.exists()


def test_live_download_keeps_its_partial_and_cancelled_one_loses_it_after_a_day(
    collector, cache, tmp_path: Path
) -> None:
    gatherer, service, clock = collector
    _, sessions = cache
    operation_id, directory = _interrupted(service, tmp_path, repair=False)
    clock.advance(days=3)
    assert gatherer.collect() == (0, False)
    assert directory.exists()
    _mark_ended(sessions, operation_id)
    # The cancelled download's bytes survive a day from their last write.
    clock.now = datetime.now(UTC) + timedelta(hours=1)
    assert gatherer.collect() == (0, False)
    assert directory.exists()
    clock.now = datetime.now(UTC) + timedelta(hours=25)
    assert gatherer.collect() == (1, False)
    assert not directory.exists()


def test_live_repair_keeps_its_partial_and_cancelled_one_loses_it(
    collector, cache, tmp_path: Path
) -> None:
    gatherer, service, clock = collector
    _, sessions = cache
    operation_id, directory = _interrupted(service, tmp_path, repair=True)
    assert directory.name.startswith("repair-")
    clock.advance(days=3)
    assert gatherer.collect() == (0, False)
    assert directory.exists()
    _mark_ended(sessions, operation_id)
    clock.now = datetime.now(UTC) + timedelta(hours=25)
    assert gatherer.collect() == (1, False)
    assert not directory.exists()


def test_held_artifact_lock_skips_the_pass_and_the_next_one_removes(collector) -> None:
    gatherer, service, clock = collector
    directory = _orphan(service, f"repair-{uuid4()}")
    clock.advance(days=2)
    lock = service.root / "locks" / ("b" * 64)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as writer:
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert gatherer.collect() == (0, False)
        assert directory.exists()
    assert gatherer.collect() == (1, False)
    assert not directory.exists()


def test_tick_runs_once_per_interval(collector) -> None:
    gatherer, service, clock = collector
    _orphan(service, "d" * 64)
    clock.advance(days=2)
    assert gatherer.tick() is True
    _orphan(service, "e" * 64)
    assert gatherer.tick() is False
    clock.advance(hours=1)
    assert gatherer.tick() is True
