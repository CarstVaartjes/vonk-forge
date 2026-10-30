"""Unfinished model downloads are collected a day after nothing writes them."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import LifecycleState
from vonk_control.model_cache import ModelCacheService, partials_collection
from vonk_control.model_cache.errors import _ArtifactWriterBusy
from vonk_control.model_cache.partial_owner_lock import partial_owner_lock
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
        ModelPartialsCollector(sessions, service, clock=clock),
        service,
        clock,
    )
    service.close()


def _orphan(service: ModelCacheService, name: str) -> Path:
    directory = service.root / "partials" / name
    directory.mkdir(parents=True)
    (directory / f"{'b' * 64}.part").write_bytes(b"unfinished")
    _age(directory)
    return directory


def _age(directory: Path) -> None:
    """Make the directory and its files look untouched for three days."""

    stale = _NOW.timestamp() - 3 * 86400
    for path in (*directory.iterdir(), directory):
        os.utime(path, (stale, stale))


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
    _age(directory)
    return operation_id, directory


def _end_row(sessions: sessionmaker[Session], operation_id: str, clock: _Clock) -> None:
    """The operation ends now: its last update is the clock's present."""

    with sessions.begin() as session:
        row = session.get(ModelCacheOperation, operation_id)
        assert row is not None
        row.state = LifecycleState.CANCELLED.value
        row.updated_at = clock.now


def _live_row(sessions: sessionmaker[Session], set_digest: str) -> None:
    with sessions.begin() as session:
        session.add(
            ModelCacheOperation(
                id=str(uuid4()),
                request_key=str(uuid4()),
                kind="download",
                state=LifecycleState.RUNNING.value,
                artifact_set_sha256=set_digest,
                payload={},
                progress={},
                actor="test",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


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
    clock.advance(hours=-72 + 23)
    assert gatherer.collect() == (0, False)
    clock.advance(hours=1)
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
    # The clock of a cancelled download starts when it was cancelled, not at
    # the last byte, which is days older.
    _end_row(sessions, operation_id, clock)
    clock.advance(hours=23, minutes=59)
    assert gatherer.collect() == (0, False)
    assert directory.exists()
    clock.advance(minutes=1)
    assert gatherer.collect() == (1, False)
    assert not directory.exists()


def test_live_repair_keeps_its_partial_and_cancelled_one_loses_it_after_a_day(
    collector, cache, tmp_path: Path
) -> None:
    gatherer, service, clock = collector
    _, sessions = cache
    operation_id, directory = _interrupted(service, tmp_path, repair=True)
    assert directory.name.startswith("repair-")
    clock.advance(days=3)
    assert gatherer.collect() == (0, False)
    assert directory.exists()
    _end_row(sessions, operation_id, clock)
    clock.advance(hours=23, minutes=59)
    assert gatherer.collect() == (0, False)
    assert directory.exists()
    clock.advance(minutes=1)
    assert gatherer.collect() == (1, False)
    assert not directory.exists()


def test_a_transfer_holding_the_owner_lock_keeps_its_directory(collector) -> None:
    gatherer, service, clock = collector
    directory = _orphan(service, f"repair-{uuid4()}")
    clock.advance(days=2)
    with partial_owner_lock(service.root, directory.name, exclusive=False):
        assert gatherer.collect() == (0, False)
        assert directory.exists()
    assert gatherer.collect() == (1, False)
    assert not directory.exists()


def test_a_transfer_starting_after_the_recheck_is_refused_until_removal_ends(
    collector, monkeypatch
) -> None:
    gatherer, service, clock = collector
    directory = _orphan(service, "d" * 64)
    clock.advance(days=2)
    original = gatherer._ownership
    refused: list[bool] = []

    def judge(name: str):
        verdict = original(name)
        # A new or reopened download arrives between the re-check and the
        # removal: it is made to wait, so it cannot write into a vanishing tree.
        try:
            with partial_owner_lock(service.root, name, exclusive=False):
                refused.append(False)
        except _ArtifactWriterBusy:
            refused.append(True)
        return verdict

    monkeypatch.setattr(gatherer, "_ownership", judge)
    assert gatherer.collect() == (1, False)
    assert refused == [True]
    assert not directory.exists()
    # The lock is free again afterwards, so the retried download proceeds.
    with partial_owner_lock(service.root, directory.name, exclusive=False):
        pass


def test_transfers_take_the_shared_owner_lock(collector, tmp_path: Path) -> None:
    """Every writer goes through ``_download_one_unique``; it holds the lock."""

    _, service, _ = collector
    artifact = _artifact(tmp_path, b"x" * (2 * 1024 * 1024 + 3))
    started = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key=str(uuid4()),
        interrupt_after_bytes=_HALF_MEBIBYTE,
    )
    seen: list[bool] = []
    original = service._download_locked

    def probe(spec, set_digest, **kwargs):
        owner = service._partial_owner(kwargs["operation_id"], set_digest)
        try:
            with partial_owner_lock(service.root, owner, exclusive=True):
                seen.append(False)
        except _ArtifactWriterBusy:
            seen.append(True)
        return original(spec, set_digest, **kwargs)

    service._download_locked = probe  # type: ignore[method-assign]
    service._run_download(started.id, force=False, interrupt_after_bytes=_HALF_MEBIBYTE)
    assert seen and all(seen)


def test_pass_resumes_from_a_cursor_with_one_query_per_stale_directory(
    collector, cache, monkeypatch
) -> None:
    gatherer, service, clock = collector
    _, sessions = cache
    kept = [f"{index:064x}" for index in range(6)]
    gone = [f"{'f' * 63}{index}" for index in range(3)]
    for name in kept:
        _age(_orphan(service, name))
        _live_row(sessions, name)
    for name in gone:
        _orphan(service, name)
    clock.advance(days=2)

    class _Time:
        """Every budget check costs one second: a pass judges one directory."""

        ticks = -1

        @classmethod
        def monotonic(cls) -> float:
            cls.ticks += 1
            return float(cls.ticks)

    monkeypatch.setattr(partials_collection, "time", _Time)
    statements: list[str] = []
    event.listen(
        sessions.kw["bind"],
        "before_cursor_execute",
        lambda *args: statements.append(args[2]),
    )
    for _ in range(len(kept) + len(gone) + 1):
        _Time.ticks = -1
        before = len(statements)
        gatherer.collect()
        assert len(statements) - before <= 1
    partials = service.root / "partials"
    assert sorted(path.name for path in partials.iterdir()) == kept


def test_tick_runs_once_per_interval(collector) -> None:
    gatherer, service, clock = collector
    _orphan(service, "d" * 64)
    clock.advance(days=2)
    assert gatherer.tick() is True
    _orphan(service, "e" * 64)
    assert gatherer.tick() is False
    clock.advance(hours=1)
    assert gatherer.tick() is True
