"""A wait repeated with no progress is counted; any movement starts over."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from vonk_control.fleet_profile_contract import FleetProfileApplicationProgress
from vonk_control.operation_blockers import ProgressAlarmCode
from vonk_control.progress_stall import (
    PROGRESS_STALL_REPEATS,
    is_stalled,
    next_stall,
    stalled_blocker,
)

START = datetime(2026, 10, 14, 2, 0, tzinfo=UTC)


def _waiting(detail: str = "recipe start payload observation is unavailable", **extra):
    document = {
        "attempt": 7,
        "retry_due_at": "2026-10-14T02:01:00Z",
        "completed_steps": 0,
        "total_steps": 2,
        "blockers": [
            {
                "code": "profile.retry_conflict",
                "detail": detail,
                "severity": "warning",
                "node_ids": [],
            }
        ],
        **extra,
    }
    return FleetProfileApplicationProgress.model_validate_json(json.dumps(document))


def _observe(previous, progress, at, *, state="running", step=0):
    return next_stall(
        previous,
        progress,
        state=state,
        current_step=step,
        current_operation_id="op-1",
        observed_at=at,
    )


def _repeat(times: int):
    stall = None
    for index in range(times):
        stall = _observe(stall, _waiting(), START + timedelta(minutes=index))
    return stall


def test_the_same_wait_counts_up_whatever_its_retry_bookkeeping() -> None:
    stall = None
    for index in range(PROGRESS_STALL_REPEATS):
        # Attempts, due times and numbers quoted in the text are not movement.
        progress = _waiting(
            f"next attempt in {index * 7}s",
            attempt=index + 1,
            retry_due_at=f"2026-10-14T02:0{index}:00Z",
        )
        stall = _observe(stall, progress, START + timedelta(minutes=index))
    assert stall is not None
    assert stall.repeats == PROGRESS_STALL_REPEATS
    assert stall.first_seen_at == START
    assert stall.detail.startswith("profile.retry_conflict: next attempt in")
    assert is_stalled(stall)
    alarm = stalled_blocker(stall)
    assert alarm.code == ProgressAlarmCode.STALLED and alarm.severity == "error"
    assert "profile.retry_conflict" in alarm.detail


def test_below_the_threshold_is_not_an_alarm() -> None:
    assert not is_stalled(_repeat(PROGRESS_STALL_REPEATS - 1))
    assert not is_stalled(None)


def test_one_observation_written_twice_counts_once() -> None:
    first = _observe(None, _waiting(), START)
    again = _observe(first, _waiting(), START)
    assert again == first and again is not None and again.repeats == 1


def test_any_movement_starts_over() -> None:
    later = START + timedelta(hours=1)
    moved = {
        "a different wait": (_waiting("manifest is unavailable"), "running", 0),
        "bytes moved": (
            _waiting(
                child_progress={"phase": "target-copy", "bytes": 10, "node_ids": []}
            ),
            "running",
            0,
        ),
        "a step completed": (_waiting(completed_steps=1), "running", 1),
        "admitted": (_waiting(), "queued", 0),
    }
    for case, (progress, state, step) in moved.items():
        stall = _observe(_repeat(9), progress, later, state=state, step=step)
        assert stall is not None, case
        assert (stall.repeats, stall.first_seen_at) == (1, later), case


def test_no_wait_or_an_ended_load_carries_no_stall() -> None:
    stalled = _repeat(9)
    idle = FleetProfileApplicationProgress()
    assert _observe(stalled, idle, START + timedelta(hours=1)) is None
    ended = _observe(stalled, _waiting(), START + timedelta(hours=1), state="cancelled")
    assert ended is None


def test_the_alarm_itself_is_not_part_of_the_wait() -> None:
    stall = _repeat(PROGRESS_STALL_REPEATS)
    assert stall is not None
    shown = _waiting()
    shown.blockers.append(stalled_blocker(stall))
    after = _observe(stall, shown, START + timedelta(hours=1))
    assert after is not None and after.repeats == PROGRESS_STALL_REPEATS + 1
