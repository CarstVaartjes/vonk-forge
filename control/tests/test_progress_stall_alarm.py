"""A load that repeats the same wait is seen as stalled, and stops being so on progress.

The alarm is informative: the load keeps its intent and keeps waiting, the
profile and the platform summary name it, and the first movement clears it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_control.models import NodeInventorySnapshot, ResourceReservation
from vonk_control.operation_blockers import ProgressAlarmCode
from vonk_control.platform_observation import PlatformObserver

from .test_attempt_residues import _load, _loop, _switch
from .test_profile_claim_selfheal import _application, _follow_due, _settle


def _alarm(load):
    return [
        item
        for item in _application(load).blockers
        if item.code == ProgressAlarmCode.STALLED
    ]


def _platform_stalled(load):
    observer = PlatformObserver(load.sessions, clock=lambda: load.now[0])
    return observer.read().stalled_applications


def test_a_repeated_wait_raises_the_alarm_and_progress_clears_it(
    tmp_path: Path,
) -> None:
    load = _load(tmp_path)
    assert _loop(load, lambda: _switch(load) is not None, complete=False, rounds=3)
    assert not _alarm(load) and not _platform_stalled(load)
    with load.sessions.begin() as session:
        for claim in session.scalars(
            select(ResourceReservation).where(ResourceReservation.kind == "disk")
        ):
            session.delete(claim)
        snapshots = list(session.scalars(select(NodeInventorySnapshot)))
        free = {row.node_id: row.disk_free_bytes for row in snapshots}
        for snapshot in snapshots:
            snapshot.disk_free_bytes = 100

    for _ in range(40):
        _loop(load, lambda: False, rounds=1)
        _follow_due(load)
        if _alarm(load):
            break
    application = _application(load)
    # Still waiting, never cancelled: the alarm sits after the wait it repeats.
    assert application.state == "running"
    (alarm,) = _alarm(load)
    assert application.blockers[0].code != ProgressAlarmCode.STALLED
    assert application.progress.stall is not None
    assert application.progress.stall.detail in alarm.detail
    (listed,) = _platform_stalled(load)
    assert listed.application_id == load.application.id
    assert listed.repeats == application.progress.stall.repeats

    with load.sessions.begin() as session:
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.disk_free_bytes = free[snapshot.node_id]
    _loop(load, lambda: not _alarm(load), rounds=10)
    assert not _alarm(load) and not _platform_stalled(load)
    assert _settle(load), _application(load).status_reason
    assert not _platform_stalled(load)


def test_a_parked_load_repeating_an_unavailable_observation_is_stalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deferred exact step (backoff on its own attempts) raises it too."""

    load = _load(tmp_path)
    assert _loop(load, lambda: _switch(load) is not None, complete=False, rounds=3)
    adapter = load.profiles._switch_adapter
    real_advance = adapter.advance

    def unavailable(operation_id, *, session):
        raise RuntimeError("recipe start payload observation is unavailable")

    monkeypatch.setattr(adapter, "advance", unavailable)
    for _ in range(40):
        load.profiles.tick()
        _follow_due(load)
        if _alarm(load):
            break
    application = _application(load)
    assert application.state in {"running", "queued"}
    (alarm,) = _alarm(load)
    assert "recipe start payload observation is unavailable" in alarm.detail
    assert application.blockers[0].code != ProgressAlarmCode.STALLED
    (listed,) = _platform_stalled(load)
    assert listed.application_id == load.application.id

    monkeypatch.setattr(adapter, "advance", real_advance)
    _loop(load, lambda: not _alarm(load), rounds=10)
    assert not _alarm(load) and not _platform_stalled(load)
    assert _settle(load), _application(load).status_reason
