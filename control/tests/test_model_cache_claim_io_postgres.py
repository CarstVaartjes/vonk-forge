"""The claim pass reads the disk before it takes a lock, never while holding one."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from sqlalchemy import text
from vonk_control.model_cache.persistence import _store_operation_payload, _updated
from vonk_control.models import ModelCacheOperation

from .test_model_cache_parallel_space_postgres import _admit, _service

FREE = [10**12]


def _lock_free(postgres_engine, operation_ids: list[str]) -> None:
    """Every row can be locked at once by another connection: nothing is held."""
    with postgres_engine.connect() as connection:
        for operation_id in operation_ids:
            connection.execute(
                text(
                    "SELECT id FROM model_cache_operations WHERE id = :id FOR UPDATE NOWAIT"
                ),
                {"id": operation_id},
            )
        connection.rollback()


def _setup(postgres_engine, tmp_path: Path, monkeypatch):
    service, sessions = _service(postgres_engine, tmp_path, monkeypatch, FREE)
    running = _admit(service, tmp_path / "a", f"{1:064x}", b"a" * 4096)
    assert service._claim_operations(limit=1, respect_backoff=False) == [
        (running, "download")
    ]
    due = _admit(service, tmp_path / "b", f"{2:064x}", b"b" * 4096)
    return service, sessions, running, due


def test_no_row_or_lock_is_held_while_the_disk_is_read(
    postgres_engine, tmp_path: Path, monkeypatch
) -> None:
    """Catches file reads inside the claim's FOR UPDATE transaction and cache lock."""
    service, _sessions, running, due = _setup(postgres_engine, tmp_path, monkeypatch)
    reads: list[str] = []
    need, live = service._transfer_need, service._running_transfers

    def probe_need(*args, **kwargs):
        reads.append("need")
        _lock_free(postgres_engine, [running, due])
        assert not cast(Any, service._lock)._is_owned()
        return need(*args, **kwargs)

    def probe_running(session):
        reads.append("running")
        _lock_free(postgres_engine, [running, due])
        assert not cast(Any, service._lock)._is_owned()
        return live(session)

    monkeypatch.setattr(service, "_transfer_need", probe_need)
    monkeypatch.setattr(service, "_running_transfers", probe_running)
    try:
        facts = service._claim_facts(respect_backoff=False)
        # What the running transfer still writes, then the due candidate's need.
        assert reads == ["running", "need", "need"]
        assert list(facts.candidates) == [due]
        assert service._claim_operations(
            limit=1, respect_backoff=False, facts=facts
        ) == [(due, "download")]
    finally:
        service.close()


def test_a_row_changed_after_its_facts_are_read_is_claimed_next_pass(
    postgres_engine, tmp_path: Path, monkeypatch
) -> None:
    """Catches a claim that trusts disk facts read for a different document."""
    service, sessions, _running, due = _setup(postgres_engine, tmp_path, monkeypatch)
    try:
        facts = service._claim_facts(respect_backoff=False)
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

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the disk was read with nothing due")

    monkeypatch.setattr(service, "_running_transfers", forbidden)
    monkeypatch.setattr(service, "_transfer_need", forbidden)
    try:
        assert service._claim_operations(limit=1, respect_backoff=True) == []
    finally:
        service.close()
