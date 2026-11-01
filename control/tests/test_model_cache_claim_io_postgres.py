"""Storage reads happen with no row locked and the cache lock free."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from vonk_agent_protocol import ModelFileState
from vonk_control.model_cache.persistence import (
    _manifest_of,
    _store_operation_payload,
    _updated,
)
from vonk_control.models import ModelCacheOperation

from .test_model_cache_parallel_space_postgres import _admit, _service

FREE = [10**12]


class _Reads:
    """Records, for each storage read, whether a row was locked or the cache lock held."""

    def __init__(self, postgres_engine, service, monkeypatch, rows, names, scopes=()):
        self.problems: list[str] = []
        self.count: dict[str, int] = {name: 0 for name in names}
        self._engine, self._service, self._rows = postgres_engine, service, rows
        self._active = 0
        self._thread = threading.current_thread()
        for name in names:
            monkeypatch.setattr(service, name, self._wrap(name, getattr(service, name)))
        for name in scopes:
            if hasattr(service, name):
                monkeypatch.setattr(service, name, self._scope(getattr(service, name)))
        if not scopes:
            self._active = 1

    def _scope(self, function):
        def run(*args, **kwargs):
            self._active += 1
            try:
                return function(*args, **kwargs)
            finally:
                self._active -= 1

        return run

    def _wrap(self, name, function):
        def read(*args, **kwargs):
            if self._active and threading.current_thread() is self._thread:
                self.count[name] += 1
                self._probe(name)
            return function(*args, **kwargs)

        return read

    def _probe(self, name: str) -> None:
        if cast(Any, self._service._lock)._is_owned():
            self.problems.append(f"{name}: read under the cache lock")
        with self._engine.connect() as connection:
            for operation_id in self._rows:
                try:
                    connection.execute(
                        text(
                            "SELECT id FROM model_cache_operations "
                            "WHERE id = :id FOR UPDATE NOWAIT"
                        ),
                        {"id": operation_id},
                    )
                except OperationalError as error:
                    self.problems.append(f"{name}: {error.orig}")
                    connection.rollback()
            connection.rollback()


def _setup(postgres_engine, tmp_path: Path, monkeypatch, due: int = 1):
    service, sessions = _service(postgres_engine, tmp_path, monkeypatch, FREE)
    running = _admit(service, tmp_path / "a", f"{1:064x}", b"a" * 4096)
    assert service._claim_operations(limit=1, respect_backoff=False) == [
        (running, "download")
    ]
    waiting = [
        _admit(service, tmp_path / f"b{n}", f"{n + 2:064x}", bytes([n + 2]) * 4096)
        for n in range(due)
    ]
    return service, sessions, running, waiting


def _running_manifest(service, running: str):
    with service._session() as session:
        operation = session.get(ModelCacheOperation, running)
        assert operation is not None
        payload = service._transfer_or_none(operation)
        assert payload is not None
        return _manifest_of(payload)


def test_a_tick_reads_no_file_under_row_locks_or_the_cache_lock(
    postgres_engine, tmp_path: Path, monkeypatch
) -> None:
    """Catches file reads inside the claim's FOR UPDATE transaction and cache lock."""
    service, _sessions, running, (due,) = _setup(postgres_engine, tmp_path, monkeypatch)
    reads = _Reads(
        postgres_engine,
        service,
        monkeypatch,
        [running, due],
        ("_transfer_need", "_object_is_stored"),
        scopes=("_claim_facts", "_claim_operations"),
    )
    try:
        assert service.tick(limit=1) >= 1
    finally:
        service.close()
    assert reads.count["_transfer_need"] >= 2  # the running transfer and the due one
    assert reads.problems == []


def test_a_failed_download_reads_its_receipts_before_locking_the_row(
    postgres_engine, tmp_path: Path, monkeypatch
) -> None:
    """Catches receipt reads inside _finish_failed's FOR UPDATE transaction."""
    service, _sessions, running, _due = _setup(postgres_engine, tmp_path, monkeypatch)
    manifest = _running_manifest(service, running)
    reads = _Reads(
        postgres_engine,
        service,
        monkeypatch,
        [running],
        ("_object_is_stored", "_stored_object"),
    )
    try:
        service._finish_failed(running, manifest.digest, manifest, RuntimeError("x"))
    finally:
        service.close()
    assert sum(reads.count.values()) >= 2
    assert reads.problems == []


def test_a_progress_checkpoint_reads_no_receipt_under_the_lock_or_row_lock(
    postgres_engine, tmp_path: Path, monkeypatch
) -> None:
    """Catches the checkpoint of every download thread reading receipts under both locks."""
    service, _sessions, running, _due = _setup(postgres_engine, tmp_path, monkeypatch)
    manifest = _running_manifest(service, running)
    reads = _Reads(
        postgres_engine, service, monkeypatch, [running], ("_stored_object",)
    )
    try:
        service._checkpoint_artifact(
            manifest.artifacts[0],
            operation_id=running,
            set_digest=manifest.digest,
            actual_bytes=10,
            state=ModelFileState.PARTIAL,
        )
    finally:
        service.close()
    assert reads.count["_stored_object"] >= 1
    assert reads.problems == []


def test_a_row_changed_after_its_facts_are_read_is_claimed_next_pass(
    postgres_engine, tmp_path: Path, monkeypatch
) -> None:
    """Catches a claim that trusts disk facts read for a different document."""
    service, sessions, _running, (due,) = _setup(postgres_engine, tmp_path, monkeypatch)
    try:
        facts = service._claim_facts(limit=1, respect_backoff=False)
        with sessions.begin() as session:
            operation = session.get(ModelCacheOperation, due)
            assert operation is not None
            payload = service._transfer_or_none(operation)
            assert payload is not None
            _store_operation_payload(
                operation, operation.kind, _updated(payload, force_refresh=True)
            )
        assert (
            service._claim_operations(limit=1, respect_backoff=False, facts=facts) == []
        )
        assert service._claim_operations(limit=1, respect_backoff=False) == [
            (due, "download")
        ]
    finally:
        service.close()


def test_nothing_due_reads_no_running_transfer(
    postgres_engine, tmp_path: Path, monkeypatch
) -> None:
    """Catches a tick that measures every running transfer with nothing to claim."""
    service, _sessions = _service(postgres_engine, tmp_path, monkeypatch, FREE)
    running = _admit(service, tmp_path / "a", f"{1:064x}", b"a" * 4096)
    assert service._claim_operations(limit=1, respect_backoff=False) == [
        (running, "download")
    ]
    reads = _Reads(postgres_engine, service, monkeypatch, [], ("_transfer_need",))
    try:
        assert service._claim_operations(limit=1, respect_backoff=True) == []
    finally:
        service.close()
    assert reads.count["_transfer_need"] == 0


def test_the_disk_is_read_for_no_more_candidates_than_the_pass_can_claim(
    postgres_engine, tmp_path: Path, monkeypatch
) -> None:
    """Catches a pass that measures every due download whatever its limit or slots."""
    service, _sessions, _running, due = _setup(
        postgres_engine, tmp_path, monkeypatch, due=6
    )
    reads = _Reads(postgres_engine, service, monkeypatch, [], ("_transfer_need",))
    try:
        assert len(service._claim_operations(limit=2, respect_backoff=False)) == 2
        limited = reads.count["_transfer_need"]
        # The running transfer's own need is read once; each claim reads one.
        assert limited <= 1 + 2
        reads.count["_transfer_need"] = 0
        assert (
            service._claim_operations(limit=4, respect_backoff=False, transfer_slots=0)
            == []
        )
        assert reads.count["_transfer_need"] == 0
    finally:
        service.close()
    assert len(due) == 6


def test_a_download_with_nothing_to_transfer_is_reached_behind_a_full_pool(
    postgres_engine, tmp_path: Path, monkeypatch
) -> None:
    """Catches the limit spent on queued byte-movers that cannot claim with no slot."""
    service, sessions, _running, movers = _setup(
        postgres_engine, tmp_path, monkeypatch, due=5
    )
    stored = _admit(service, tmp_path / "stored", f"{99:064x}", b"s" * 4096)
    with sessions.begin() as session:
        operation = session.get(ModelCacheOperation, stored)
        assert operation is not None
        payload = service._transfer_or_none(operation)
        assert payload is not None
        _store_operation_payload(
            operation,
            operation.kind,
            _updated(
                payload,
                transfer=payload.transfer.model_copy(update={"total_bytes": 0}),
            ),
        )
    monkeypatch.setattr(service, "_object_is_stored", lambda _spec: True)
    try:
        assert service._claim_operations(
            limit=4, respect_backoff=False, transfer_slots=0
        ) == [(stored, "download")]
    finally:
        service.close()
    assert len(movers) == 5


def test_an_operation_whose_manifest_does_not_read_is_retired(
    postgres_engine, tmp_path: Path, monkeypatch
) -> None:
    """Catches a candidate skipped on every tick instead of ended with a typed reason."""
    service, sessions, _running, (damaged,) = _setup(
        postgres_engine, tmp_path, monkeypatch
    )

    def unreadable(_payload):
        raise ValueError("manifest does not read")

    monkeypatch.setattr("vonk_control.model_cache.scheduler._manifest_of", unreadable)
    try:
        assert service._claim_operations(limit=1, respect_backoff=False) == []
    finally:
        service.close()
    with sessions() as session:
        operation = session.get(ModelCacheOperation, damaged)
        assert operation is not None
        assert operation.state == "failed"
        assert "persisted-state-damaged" in (operation.last_error or "")
