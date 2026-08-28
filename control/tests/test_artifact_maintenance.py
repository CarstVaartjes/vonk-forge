from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import UTC, datetime, timedelta

import pytest
from vonk_control.artifact_maintenance import ArtifactMaintenanceCadence


def test_artifact_maintenance_cadence_is_durable_across_process_instances(
    tmp_path,
) -> None:
    current = datetime(2026, 8, 28, 12, tzinfo=UTC)
    calls: list[int] = []

    def reconcile(*, batch_limit: int):
        calls.append(batch_limit)
        return {"expired_jobs": 2, "removed_orphan_blobs": 1}

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

    def reconcile(*, batch_limit: int):
        nonlocal calls
        assert batch_limit == 10
        calls += 1
        raise OSError("CAS unavailable")

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
    assert "artifact storage reconciliation failed" in caplog.text
    state = json.loads((tmp_path / ".maintenance.json").read_text())
    assert state["last_failure_at"] == current.isoformat()
    assert state["last_failure_type"] == "OSError"


def test_artifact_maintenance_never_waits_for_another_process(tmp_path) -> None:
    current = datetime(2026, 8, 28, 12, tzinfo=UTC)
    calls = 0

    def reconcile(*, batch_limit: int):
        nonlocal calls
        assert batch_limit == 1
        calls += 1
        return {}

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
        lambda **_kwargs: {},
        state_root=tmp_path,
        interval_seconds=60,
        batch_limit=1,
        clock=lambda: datetime(2026, 8, 28, 12),  # noqa: DTZ001
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        cadence()
