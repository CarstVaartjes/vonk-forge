"""Unfinished model downloads are collected a day after nothing writes them."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import LifecycleState, ModelCacheCode
from vonk_control.model_cache import ModelCacheService, partials_collection
from vonk_control.model_cache.errors import _ArtifactWriterBusy
from vonk_control.model_cache.partials_collection import ModelPartialsCollector
from vonk_control.model_cache.split_transfer import REPAIR_PARTIALS_PREFIX
from vonk_control.models import ModelCacheOperation

from .removal_settle import advance_until_ended
from .test_model_cache import _artifact, _download, _remove_model, cache

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


def _interrupted(service: ModelCacheService, tmp_path: Path) -> tuple[str, Path]:
    artifact = _artifact(tmp_path, b"x" * (2 * 1024 * 1024 + 3))
    started = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key=str(uuid4()),
        interrupt_after_bytes=_HALF_MEBIBYTE,
    )
    service._run_download(started.id, force=False, interrupt_after_bytes=_HALF_MEBIBYTE)
    assert service.get_operation(started.id).state == LifecycleState.BACKOFF
    (directory,) = (service.root / "partials").iterdir()
    _age(directory)
    return started.id, directory


def _cancel(
    service: ModelCacheService,
    sessions: sessionmaker[Session],
    operation_id: str,
    clock: _Clock,
) -> None:
    """Cancel through the service; the collector's clock starts at that update."""

    service.cancel_operation(
        operation_id, actor="test", request_key=str(uuid4()), reason="no longer wanted"
    )
    assert service.get_operation(operation_id).state == LifecycleState.CANCELLED
    clock.now = _updated_at(sessions, operation_id)


def _updated_at(sessions: sessionmaker[Session], operation_id: str) -> datetime:
    with sessions() as session:
        row = session.get(ModelCacheOperation, operation_id)
        assert row is not None
        return row.updated_at.replace(tzinfo=UTC)


def _request_fresh(service: ModelCacheService, tmp_path: Path) -> None:
    """A new request after an ending is admitted and completes, never blocked."""

    again = _download(
        service,
        [_artifact(tmp_path, b"asked again", artifact_id="again", path="again.bin")],
        model_content_sha256="b" * 64,
        request_key=str(uuid4()),
    )
    assert again.state == LifecycleState.SUCCEEDED


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
    operation_id, directory = _interrupted(service, tmp_path)
    clock.advance(days=3)
    assert gatherer.collect() == (0, False)
    assert directory.exists()
    # The clock of a cancelled download starts when it was cancelled, not at
    # the last byte, which is days older.
    _cancel(service, sessions, operation_id, clock)
    clock.advance(hours=23, minutes=59)
    assert gatherer.collect() == (0, False)
    assert directory.exists()
    clock.advance(minutes=1)
    assert gatherer.collect() == (1, False)
    assert not directory.exists()
    _request_fresh(service, tmp_path)


def test_live_repair_keeps_its_partial_and_a_failed_one_loses_it_a_day_later(
    tmp_path: Path,
) -> None:
    """A repair cannot be cancelled; it ends by failing, which starts the clock."""

    import httpx2

    from .test_model_cache_availability_regressions import (
        _artifact as http_artifact,
    )
    from .test_model_cache_availability_regressions import (
        _database,
        _service,
        _start,
    )

    sessions = _database(tmp_path)
    body = b"x" * 100
    serving = [True]

    def handler(request):
        if serving[0]:
            return httpx2.Response(200, request=request, content=body)
        return httpx2.Response(404, request=request)

    service, _client = _service(tmp_path, sessions, handler=handler)
    clock = _Clock()
    gatherer = ModelPartialsCollector(sessions, service, clock=clock)
    artifact = http_artifact("a", body)
    downloaded = _start(service, [artifact], str(uuid4()))
    service._run_download(downloaded.id, force=False)
    assert service.get_operation(downloaded.id).state == LifecycleState.SUCCEEDED
    set_digest = str(downloaded.artifact_set_sha256)
    for leftover in (service.root / "partials").iterdir():
        leftover.rmdir()
    serving[0] = False
    preview = service.repair_preview(set_digest)
    repair = service.start_repair(
        actor="test",
        request_key=str(uuid4()),
        artifact_set_sha256=set_digest,
        plan_digest=str(preview["plan_digest"]),
    )
    owner = service._partial_owner(repair.id, set_digest)
    assert owner.startswith(REPAIR_PARTIALS_PREFIX)
    directory = service.root / "partials" / owner
    directory.mkdir(parents=True)
    (directory / f"{artifact['sha256']}.part").write_bytes(b"xxxx")
    _age(directory)
    service._run_download(repair.id, force=True)
    assert service.get_operation(repair.id).state == LifecycleState.QUEUED
    clock.advance(days=3)
    assert gatherer.collect() == (0, False)
    assert directory.exists()
    for _ in range(12):
        service._run_download(repair.id, force=True)
        if service.get_operation(repair.id).state == LifecycleState.FAILED:
            break
    failure = service.get_operation(repair.id).failure
    assert failure is not None and failure["code"] == ModelCacheCode.SOURCE_GONE
    ended = _updated_at(sessions, repair.id)
    clock.now = ended + timedelta(hours=23, minutes=59)
    assert gatherer.collect() == (0, False)
    assert directory.exists()
    clock.now = ended + timedelta(hours=24)
    assert gatherer.collect() == (1, False)
    assert not directory.exists()
    service.close()


def test_a_transfer_holding_the_owner_lock_keeps_its_directory(collector) -> None:
    gatherer, service, clock = collector
    directory = _orphan(service, f"repair-{uuid4()}")
    clock.advance(days=2)
    with service._model_storage_lock(directory.name, model_set=True, shared=True):
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
            with service._model_storage_lock(name, model_set=True, shared=True):
                refused.append(False)
        except _ArtifactWriterBusy:
            refused.append(True)
        return verdict

    monkeypatch.setattr(gatherer, "_ownership", judge)
    assert gatherer.collect() == (1, False)
    assert refused == [True]
    assert not directory.exists()
    # The lock is free again afterwards, so the retried download proceeds.
    with service._model_storage_lock(directory.name, model_set=True, shared=True):
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
            with service._model_storage_lock(owner, model_set=True):
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


def test_a_failed_download_keeps_its_partial_a_day_from_the_failure(tmp_path: Path):
    """A real terminal failure (the source is gone) starts the same clock."""

    import httpx2

    from .test_model_cache_availability_regressions import (
        _artifact as http_artifact,
    )
    from .test_model_cache_availability_regressions import (
        _database,
        _service,
        _start,
    )

    sessions = _database(tmp_path)
    service, _client = _service(
        tmp_path,
        sessions,
        handler=lambda request: httpx2.Response(404, request=request),
    )
    clock = _Clock()
    gatherer = ModelPartialsCollector(sessions, service, clock=clock)
    artifact = http_artifact("a", b"x" * 100)
    started = _start(service, [artifact], str(uuid4()))
    directory = service.root / "partials" / str(started.artifact_set_sha256)
    directory.mkdir(parents=True)
    (directory / f"{artifact['sha256']}.part").write_bytes(b"xxxx")
    _age(directory)
    for _ in range(12):
        service._run_download(started.id, force=False)
        if service.get_operation(started.id).state == LifecycleState.FAILED:
            break
    failure = service.get_operation(started.id).failure
    assert failure is not None and failure["code"] == ModelCacheCode.SOURCE_GONE
    ended = _updated_at(sessions, started.id)
    clock.now = ended + timedelta(hours=23, minutes=59)
    assert gatherer.collect() == (0, False)
    assert directory.exists()
    clock.now = ended + timedelta(hours=24)
    assert gatherer.collect() == (1, False)
    assert not directory.exists()
    service.close()


def test_a_download_arriving_while_partials_are_removed_is_deferred_then_completes(
    collector, cache, tmp_path: Path, monkeypatch
) -> None:
    gatherer, service, clock = collector
    _, sessions = cache
    artifact = _artifact(tmp_path, b"y" * (2 * 1024 * 1024 + 5))
    first = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key=str(uuid4()),
        interrupt_after_bytes=_HALF_MEBIBYTE,
    )
    service._run_download(first.id, force=False, interrupt_after_bytes=_HALF_MEBIBYTE)
    (directory,) = (service.root / "partials").iterdir()
    _age(directory)
    _cancel(service, sessions, first.id, clock)
    clock.advance(hours=25)
    original = gatherer._ownership
    arrivals: list[str] = []

    def judge(name: str):
        verdict = original(name)
        # The collector holds the lock and has judged the directory abandoned
        # when the user asks for the same set again.
        second = _download(
            service,
            [artifact],
            model_content_sha256="a" * 64,
            request_key=str(uuid4()),
            interrupt_after_bytes=_HALF_MEBIBYTE,
        )
        service._run_download(second.id, force=False)
        arrivals.append(second.id)
        return verdict

    monkeypatch.setattr(gatherer, "_ownership", judge)
    assert gatherer.collect() == (1, False)
    monkeypatch.undo()
    (second_id,) = arrivals
    # Refused while the lock was held: a wait, not a failure, and no bytes.
    deferred = service.get_operation(second_id)
    assert deferred.state in {LifecycleState.QUEUED, LifecycleState.BACKOFF}
    assert not directory.exists()
    # The reschedule runs once the lock is free and the download completes.
    service._clock = lambda: datetime.now(UTC) + timedelta(hours=1)
    for _ in range(8):
        service.run_pending()
        if service.get_operation(second_id).state == LifecycleState.SUCCEEDED:
            break
    assert service.get_operation(second_id).state == LifecycleState.SUCCEEDED
    _request_fresh(service, tmp_path)


def test_set_removal_defers_while_a_transfer_holds_the_lock_then_completes(
    cache, tmp_path: Path
) -> None:
    service, _ = cache
    artifact = _artifact(tmp_path, b"z" * 64)
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key=str(uuid4()),
    )
    set_digest = str(downloaded.artifact_set_sha256)
    removal = _remove_model(
        service,
        "a" * 64,
        actor="operator",
        request_key=str(uuid4()),
        model_content_sha256="a" * 64,
    )
    with service._model_storage_lock(set_digest, model_set=True, shared=True):
        advance_until_ended(service, removal.id, rounds=4)
        assert service.get_operation(removal.id).state in {
            LifecycleState.QUEUED,
            LifecycleState.BACKOFF,
            LifecycleState.RUNNING,
        }
    advance_until_ended(service, removal.id)
    assert service.get_operation(removal.id).state == LifecycleState.SUCCEEDED
    _request_fresh(service, tmp_path)
    service.close()


def test_a_failed_query_or_unreadable_directory_does_not_stop_the_pass(
    collector, monkeypatch
) -> None:
    gatherer, service, clock = collector
    broken_query = _orphan(service, "1" * 64)
    unreadable = _orphan(service, "2" * 64)
    healthy = _orphan(service, "3" * 64)
    clock.advance(days=2)
    original_owner = gatherer._ownership
    original_change = gatherer._newest_change

    def ownership(name: str):
        if name == broken_query.name:
            raise OperationalError("select", {}, RuntimeError("database away"))
        return original_owner(name)

    def newest_change(name: str):
        if name == unreadable.name:
            raise PermissionError("not readable")
        return original_change(name)

    monkeypatch.setattr(gatherer, "_ownership", ownership)
    monkeypatch.setattr(gatherer, "_newest_change", newest_change)
    assert gatherer.collect() == (1, False)
    assert broken_query.exists() and unreadable.exists()
    assert not healthy.exists()
    monkeypatch.undo()
    # Both heal on the next pass.
    assert gatherer.collect() == (2, False)


def test_a_writer_without_the_owner_lock_fails_loudly(collector, tmp_path: Path):
    _, service, _ = collector
    artifact = _artifact(tmp_path, b"w" * 64)
    started = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key=str(uuid4()),
        interrupt_after_bytes=_HALF_MEBIBYTE,
    )
    manifest, set_digest, _force, _transfer = service._start_transfer(
        started.id, force=False
    )
    spec = next(iter(manifest.artifacts))
    kwargs = {
        "operation_id": started.id,
        "completed_artifacts": 0,
        "force": False,
        "interrupt_after_bytes": None,
    }
    with pytest.raises(RuntimeError, match="without its lock"):
        service._download_artifact(spec, set_digest, owner=set_digest, **kwargs)
    assert not list((service.root / "partials").glob("**/*.part"))
    # Holding the lock is what admits the writer.
    with service._model_storage_lock(set_digest, model_set=True, shared=True):
        require = __import__(
            "vonk_control.model_cache.split_transfer", fromlist=["x"]
        ).require_partials_owner_lock
        require(set_digest)
