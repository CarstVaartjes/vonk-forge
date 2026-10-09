from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import UTC, datetime, timedelta

import pytest
from vonk_control.artifact_jobs import StorageReconciliation
from vonk_control.artifact_maintenance import ArtifactMaintenanceCadence


def _report(**counts: int) -> StorageReconciliation:
    return StorageReconciliation.model_validate(
        {
            "max_stored_bytes": 0,
            "used_bytes": 0,
            "reserved_bytes": 0,
            "in_flight_uploads": 0,
            "remaining_bytes": 0,
            "removed_temporary_files": 0,
            "removed_reservation_files": 0,
            "removed_orphan_blobs": 0,
            "missing_referenced_blobs": [],
            "remaining_work": False,
            "expired_jobs": 0,
            "removed_blob_records": 0,
            **counts,
        }
    )


def test_artifact_maintenance_cadence_is_durable_across_process_instances(
    tmp_path,
) -> None:
    current = datetime(2026, 8, 28, 12, tzinfo=UTC)
    calls: list[int] = []

    def reconcile(*, batch_limit: int):
        calls.append(batch_limit)
        return _report(expired_jobs=2, removed_orphan_blobs=1)

    first = ArtifactMaintenanceCadence(
        reconcile,
        state_root=tmp_path,
        interval_seconds=60,
        batch_limit=25,
        clock=lambda: current,
    )
    second = ArtifactMaintenanceCadence(
        reconcile,
        state_root=tmp_path,
        interval_seconds=60,
        batch_limit=25,
        clock=lambda: current,
    )

    first()
    second()
    assert calls == []

    current += timedelta(seconds=60)
    first()
    second()
    assert calls == [25]

    state = json.loads((tmp_path / ".maintenance.json").read_text())
    assert state["last_attempt_at"] == current.isoformat()
    assert state["last_success_at"] == current.isoformat()
    assert state["next_due_at"] == (current + timedelta(seconds=60)).isoformat()


def test_artifact_maintenance_failure_is_logged_and_rate_limited(
    tmp_path,
    caplog,
) -> None:
    current = datetime(2026, 8, 28, 12, tzinfo=UTC)
    calls = 0
    unavailable = True

    def reconcile(*, batch_limit: int):
        nonlocal calls
        assert batch_limit == 10
        calls += 1
        if unavailable:
            raise OSError("CAS unavailable")
        return _report()

    cadence = ArtifactMaintenanceCadence(
        reconcile,
        state_root=tmp_path,
        interval_seconds=60,
        batch_limit=10,
        clock=lambda: current,
    )
    cadence()
    current += timedelta(seconds=60)
    with caplog.at_level(logging.ERROR):
        cadence()
        cadence()

    assert calls == 1
    state = json.loads((tmp_path / ".maintenance.json").read_text())
    assert state["last_failure_at"] == current.isoformat()
    unavailable = False
    current += timedelta(seconds=60)
    cadence()
    assert calls == 2
    state = json.loads((tmp_path / ".maintenance.json").read_text())
    assert state["last_success_at"] == current.isoformat()
    current += timedelta(seconds=60)
    cadence()
    assert calls == 3


def test_artifact_maintenance_never_waits_for_another_process(tmp_path) -> None:
    current = datetime(2026, 8, 28, 12, tzinfo=UTC)
    calls = 0

    def reconcile(*, batch_limit: int):
        nonlocal calls
        assert batch_limit == 1
        calls += 1
        return _report()

    cadence = ArtifactMaintenanceCadence(
        reconcile,
        state_root=tmp_path,
        interval_seconds=60,
        batch_limit=1,
        clock=lambda: current,
    )
    cadence()
    current += timedelta(seconds=60)
    descriptor = os.open(tmp_path / ".maintenance.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cadence()
        assert calls == 0
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    current += timedelta(seconds=1)
    cadence()
    assert calls == 1


def test_artifact_maintenance_rejects_unaware_clock(tmp_path) -> None:
    cadence = ArtifactMaintenanceCadence(
        lambda **_kwargs: _report(),
        state_root=tmp_path,
        interval_seconds=60,
        batch_limit=1,
        clock=lambda: datetime(2026, 8, 28, 12),  # noqa: DTZ001
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        cadence()


@pytest.mark.parametrize(
    "damage",
    [
        "{",
        '{"next_due_at":"broken"}',
        '{"next_due_at":"2026-08-28T12:00:00"}',
        '{"next_due_at":7}',
        '{"next_due_at":"2999-08-28T12:00:00+00:00"}',
        "\\xff",
    ],
)
def test_damaged_cadence_reconciles_then_a_new_instance_observes_the_schedule(
    tmp_path, damage
):
    """Catches retrying an unreadable cadence forever without normal store work."""
    current = datetime(2026, 8, 28, 12, tzinfo=UTC)
    calls = []
    retained = tmp_path / "verified-content"
    retained.write_bytes(b"verified")
    (tmp_path / ".maintenance.json").write_bytes(
        damage.encode() if damage != "\\xff" else b"\xff"
    )

    def reconcile(*, batch_limit):
        calls.append(batch_limit)
        return _report()

    def instance():
        return ArtifactMaintenanceCadence(
            reconcile,
            state_root=tmp_path,
            interval_seconds=60,
            batch_limit=2,
            clock=lambda: current,
        )

    instance()()
    assert calls == [2]
    assert retained.read_bytes() == b"verified"
    instance()()
    assert calls == [2]
    current += timedelta(seconds=60)
    instance()()
    assert calls == [2, 2]
    assert retained.read_bytes() == b"verified"


def test_cadence_write_fault_does_not_suppress_reconciliation(tmp_path, monkeypatch):
    """Catches a failed scheduling projection becoming an artifact work gate."""
    current = datetime(2026, 8, 28, 12, tzinfo=UTC)
    calls = []
    cadence = ArtifactMaintenanceCadence(
        lambda **kwargs: calls.append(kwargs["batch_limit"]) or _report(),
        state_root=tmp_path,
        interval_seconds=60,
        batch_limit=1,
        clock=lambda: current,
    )
    cadence()
    original = cadence._write_state

    def damaged(_state):
        raise OSError("cadence storage unavailable")

    monkeypatch.setattr(cadence, "_write_state", damaged)
    current += timedelta(seconds=60)
    cadence()
    cadence()
    assert calls == [1]
    monkeypatch.setattr(cadence, "_write_state", original)
    current += timedelta(seconds=60)
    cadence()
    assert calls == [1, 1]
    state = json.loads((tmp_path / ".maintenance.json").read_text())
    assert state["last_success_at"] == current.isoformat()


def test_unreadable_cadence_link_is_replaced_without_touching_its_target(tmp_path):
    """Catches following/deleting artifact bytes to repair cadence bookkeeping."""
    retained = tmp_path / "verified-content"
    retained.write_bytes(b"verified")
    state = tmp_path / ".maintenance.json"
    state.symlink_to(retained)
    calls = []
    cadence = ArtifactMaintenanceCadence(
        lambda **kwargs: calls.append(kwargs["batch_limit"]) or _report(),
        state_root=tmp_path,
        interval_seconds=60,
        batch_limit=1,
        clock=lambda: datetime(2026, 8, 28, 12, tzinfo=UTC),
    )
    cadence()
    assert calls == [1]
    assert retained.read_bytes() == b"verified"
    assert not state.is_symlink()
    cadence()
    assert calls == [1]
