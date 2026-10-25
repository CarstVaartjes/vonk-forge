"""A wait with no movement for a stall window is an alarm; any movement starts over."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from vonk_agent_protocol import (
    LifecycleState,
    ProfileReasonCode,
    RecipeImageCode,
    WaitReason,
)
from vonk_agent_protocol.agent_words import ProfileChildPhase
from vonk_control.fleet_profile_contract import FleetProfileApplicationProgress
from vonk_control.lifecycle.image_availability import PREPARATION_STALL_WINDOW
from vonk_control.operation_blockers import ProgressAlarmCode, make_blocker
from vonk_control.progress_stall import (
    PROGRESS_STALL_WINDOW,
    is_stalled,
    next_stall,
    progress_signature,
    stalled_blocker,
)

START = datetime(2026, 10, 14, 2, 0, tzinfo=UTC)
#: The retry attempts a wait spends in two minutes of the bounded backoff.
BURST = 5


def _waiting(detail: str = "recipe start payload observation is unavailable", **extra):
    document = {
        "attempt": 7,
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


def _observe(previous, progress, at, *, before=None, state="running", step=0):
    """One write of ``progress``; ``before`` defaults to the previous attempt."""

    if before is None:
        before = progress.model_copy(update={"attempt": progress.attempt - 1})
    return next_stall(
        previous,
        progress,
        before=before,
        state=state,
        current_step=step,
        current_operation_id="op-1",
        observed_at=at,
    )


def _repeat(times: int):
    """``times`` retry attempts of the same wait, one minute apart."""

    stall = None
    for index in range(times):
        progress = _waiting(attempt=index + 2)
        stall = _observe(stall, progress, START + timedelta(minutes=index))
    return stall


def test_the_same_wait_is_an_alarm_after_a_whole_window_without_movement() -> None:
    stall = None
    for index in range(BURST):
        # Attempts and numbers quoted in the text are not movement.
        progress = _waiting(
            f"next attempt in {index * 7}s",
            attempt=index + 1,
        )
        stall = _observe(stall, progress, START + timedelta(minutes=index))
    assert stall is not None
    assert stall.repeats == BURST
    assert stall.first_seen_at == START
    assert stall.detail.startswith("profile.retry_conflict: next attempt in")
    # Retry attempts are not evidence: within the window it is only a wait.
    assert not is_stalled(
        stall, now=START + PROGRESS_STALL_WINDOW - timedelta(seconds=1)
    )
    assert is_stalled(stall, now=START + PROGRESS_STALL_WINDOW)
    alarm = stalled_blocker(stall)
    assert alarm.code == ProgressAlarmCode.STALLED and alarm.severity == "error"
    assert stall.detail in alarm.detail
    assert not is_stalled(None, now=START + PROGRESS_STALL_WINDOW)


def _preparing(
    attempt: int, progressed_at: datetime, code: str
) -> FleetProfileApplicationProgress:
    """A load waiting to be admitted while the Controller prepares its assets."""

    return _waiting(
        admission_pending=True,
        admission_attempt=attempt,
        preparation_progressed_at=progressed_at.isoformat(),
        blockers=[
            {
                "code": ProfileReasonCode.PREPARATION_UNAVAILABLE.value,
                "detail": "The exact model and runtime image are not in the "
                "Controller cache yet.",
                "severity": "error",
                "node_ids": [],
            },
            {
                "code": code,
                "detail": "Preparing the model and runtime image.",
                "severity": "info",
                "node_ids": [],
            },
        ],
    )


@pytest.mark.parametrize(
    "code", [RecipeImageCode.PREPARING.value, RecipeImageCode.BUILD_WAIT.value]
)
def test_a_long_preparation_that_advances_is_never_stalled(code: str) -> None:
    # The 10-15 incident: the load retried every minute for hours while its
    # download or build advanced, sometimes only every ten minutes (a long
    # build step reports nothing in between).
    stall = None
    before = _preparing(1, START, code)
    for minute in range(1, 4 * 60):
        at = START + timedelta(minutes=minute)
        advanced = START + timedelta(minutes=minute // 10 * 10)
        progress = _preparing(minute + 1, advanced, code)
        stall = _observe(stall, progress, at, before=before)
        before = progress
        assert not is_stalled(stall, now=at + timedelta(seconds=59)), minute


@pytest.mark.parametrize(
    "code", [RecipeImageCode.PREPARING.value, RecipeImageCode.BUILD_WAIT.value]
)
def test_a_preparation_that_stopped_advancing_is_stalled_after_the_window(
    code: str,
) -> None:
    stall = None
    before = _preparing(1, START, code)
    for minute in range(1, 40):
        at = START + timedelta(minutes=minute)
        progress = _preparing(minute + 1, START, code)
        stall = _observe(stall, progress, at, before=before)
        before = progress
    assert stall is not None and stall.first_seen_at == START + timedelta(minutes=1)
    assert not is_stalled(stall, now=stall.first_seen_at + timedelta(minutes=29))
    assert is_stalled(stall, now=stall.first_seen_at + PROGRESS_STALL_WINDOW)


def test_the_window_outlasts_a_preparations_own_stall_window() -> None:
    # A preparation that stops working ends itself (and so changes the wait)
    # before its load could be called stalled for waiting on it.
    assert PROGRESS_STALL_WINDOW > PREPARATION_STALL_WINDOW


def test_one_observation_written_twice_counts_once() -> None:
    first = _observe(None, _waiting(), START)
    again = _observe(first, _waiting(), START)
    assert again == first and again is not None and again.repeats == 1


def _child_retrying(attempt: int, **extra):
    child = {"phase": "prepare", "node_ids": [], "retry_attempt": attempt}
    return _waiting(child_progress=child, **extra)


def test_re_checks_within_one_attempt_never_count() -> None:
    # Today's false alarm: the child's wait mirrored every few seconds (the due
    # time moving each time) while neither the load nor its child retried.
    stall = None
    progress = _child_retrying(3)
    for index in range(25):
        before = progress
        progress = _child_retrying(3)
        stall = _observe(
            stall, progress, START + timedelta(seconds=6 * index), before=before
        )
    assert stall is not None and stall.repeats == 1
    assert stall.first_seen_at == START


def test_every_child_retry_attempt_counts() -> None:
    # A child that keeps retrying its phase with the same cause, the load's own
    # attempt untouched (a capacity or evidence wait that never resolves).
    stall = None
    before = _child_retrying(2)
    for index in range(BURST):
        progress = _child_retrying(index + 3)
        stall = _observe(
            stall, progress, START + timedelta(minutes=index), before=before
        )
        before = progress
    assert stall is not None and stall.repeats == BURST
    assert is_stalled(stall, now=START + PROGRESS_STALL_WINDOW)


def test_every_admission_attempt_counts_too() -> None:
    stall = None
    before = _waiting(admission_pending=True)
    for index in range(BURST):
        progress = _waiting(admission_pending=True, admission_attempt=index + 1)
        stall = _observe(
            stall, progress, START + timedelta(minutes=index), before=before
        )
        before = progress
    assert stall is not None and stall.repeats == BURST
    assert is_stalled(stall, now=START + PROGRESS_STALL_WINDOW)


def test_an_unknown_previous_attempt_is_not_counted() -> None:
    first = _observe(None, _waiting(attempt=2), START)
    unknown = next_stall(
        first,
        _waiting(attempt=3),
        before=None,
        state="running",
        current_step=0,
        current_operation_id="op-1",
        observed_at=START + timedelta(minutes=1),
    )
    assert unknown == first


def test_the_post_stop_inventory_wait_reads_the_same_across_its_timestamps() -> None:
    # The exact text of ``RunSwitchPostStopEvidencePending`` as the child's
    # phase retry names it: the inventory time in the text is not movement.
    node = "spk_" + "0a1b2c3d" * 4
    run = "6f1c2a9e-1d2b-4c3d-9e8f-0a1b2c3d4e5f"
    threshold = datetime(2026, 10, 14, 11, 21, 26, 512340, tzinfo=UTC)

    def detail(latest: datetime) -> str:
        return (
            "run-switch.post-stop-inventory-pending: "
            f"Spark {node} needs inventory collected after stop {run} "
            f"({threshold.isoformat()}; latest {latest.isoformat()})"
        )

    def signature(latest: datetime) -> str:
        document = {
            "blockers": [
                {
                    "code": "run-switch.phase-retry",
                    "detail": detail(latest),
                    "severity": "warning",
                    "node_ids": [node],
                }
            ]
        }
        return progress_signature(
            FleetProfileApplicationProgress.model_validate_json(json.dumps(document)),
            state="running",
            current_step=0,
            current_operation_id="op-1",
        )

    first = signature(datetime(2026, 10, 14, 11, 19, 58, 120004, tzinfo=UTC))
    assert signature(datetime(2026, 10, 14, 11, 20, 0, tzinfo=UTC)) == first
    assert signature(datetime(2026, 10, 14, 11, 21, 59, 7, tzinfo=UTC)) == first


def test_any_movement_starts_over() -> None:
    later = START + timedelta(hours=1)
    moved = {
        "a different wait": (
            _waiting(
                blockers=[
                    {
                        "code": ProfileReasonCode.ADMISSION_BUSY.value,
                        "detail": "admission is busy",
                        "severity": "warning",
                        "node_ids": [],
                    }
                ]
            ),
            "running",
            0,
        ),
        "bytes moved": (
            _waiting(
                child_progress={"phase": "target-copy", "bytes": 10, "node_ids": []}
            ),
            "running",
            0,
        ),
        "a step completed": (_waiting(completed_steps=1), "running", 1),
        "a preparation progressed": (
            _waiting(preparation_progressed_at=later.isoformat()),
            "running",
            0,
        ),
        "admitted": (_waiting(), "queued", 0),
    }
    for case, (progress, state, step) in moved.items():
        stall = _observe(_repeat(9), progress, later, state=state, step=step)
        assert stall is not None, case
        assert (stall.repeats, stall.first_seen_at) == (1, later), case


def _child_at(attempt: int, phase: str, copied: int, at: datetime):
    """The load waiting while its child reports ``phase`` with ``copied`` bytes,
    its operation's last-progress time fresh at ``at``."""

    child = {
        "phase": phase,
        "bytes": copied,
        "node_ids": [],
        "retry_attempt": attempt,
        "operation": {
            "phase": phase,
            "completed_bytes": copied,
            "total_bytes_known": False,
            "last_progress_at": at.isoformat(),
            "activity": "active",
            "members": [],
        },
    }
    return _waiting(child_progress=child)


def _follow(samples):
    """Observe ``(at, progress)`` samples in order, each a new retry attempt."""

    stall, before = None, None
    for at, progress in samples:
        stall = _observe(stall, progress, at, before=before)
        before = progress
        yield at, stall


def test_a_copy_retried_from_zero_below_its_old_mark_is_stalled() -> None:
    # Production 2026-10-24 class: the child retries its copy, bytes fall back
    # to zero and rise again, never past what the first try reached.  Every
    # sample changed the bytes and the last-progress time, so the old
    # signature started the stall over each time and it never alarmed.
    def samples():
        attempt = 2
        for minute in range(11):  # the first try reaches 100 bytes
            attempt += 1
            at = START + timedelta(minutes=minute)
            yield (
                at,
                _child_at(
                    attempt, ProfileChildPhase.TARGET_COPY.value, minute * 10, at
                ),
            )
        for minute in range(11, 3 * 60):  # retried: 0..90, over and over
            attempt += 1
            at = START + timedelta(minutes=minute)
            yield (
                at,
                _child_at(
                    attempt, ProfileChildPhase.TARGET_COPY.value, (minute % 10) * 10, at
                ),
            )

    last_high = START + timedelta(minutes=10)
    for at, stall in _follow(samples()):
        assert stall is not None
        assert is_stalled(stall, now=at) == (at >= last_high + PROGRESS_STALL_WINDOW)
    assert stall.first_seen_at == last_high


def test_a_child_flapping_between_two_phases_is_stalled() -> None:
    # The child alternates between two phases it already showed (each with a
    # fresh last-progress time): no phase is new, so the wait does not move.
    def samples():
        for index in range(60):
            at = START + timedelta(minutes=3 * index)
            phase = (
                ProfileChildPhase.PREPARE.value
                if index % 2 == 0
                else ProfileChildPhase.RUNTIME_INSTALL.value
            )
            yield at, _child_at(index + 3, phase, 0, at)

    second_phase = START + timedelta(minutes=3)
    for at, stall in _follow(samples()):
        assert stall is not None
        assert is_stalled(stall, now=at) == (at >= second_phase + PROGRESS_STALL_WINDOW)


def test_a_copy_that_keeps_reaching_new_highs_is_never_stalled() -> None:
    # A real copy, retried once from zero: it passes its old mark and keeps
    # rising (every ten minutes), so it waits for hours and is never stalled.
    def samples():
        for minute in range(4 * 60):
            at = START + timedelta(minutes=minute)
            copied = minute // 10 * 10 if minute != 60 else 0
            yield (
                at,
                _child_at(minute + 3, ProfileChildPhase.TARGET_COPY.value, copied, at),
            )

    for at, stall in _follow(samples()):
        assert stall is not None
        assert not is_stalled(stall, now=at + timedelta(minutes=9)), at


def test_a_fresh_last_progress_time_alone_is_not_movement() -> None:
    first = _observe(None, _child_at(3, ProfileChildPhase.START.value, 0, START), START)
    later = START + timedelta(minutes=20)
    again = _observe(
        first,
        _child_at(4, ProfileChildPhase.START.value, 0, later),
        later,
        before=_child_at(3, ProfileChildPhase.START.value, 0, START),
    )
    assert again is not None and again.first_seen_at == START
    assert again.repeats == 2


def test_a_changing_symptom_of_the_same_wait_is_not_movement() -> None:
    # Seen on a load stuck for disk: every few minutes its child re-checks the
    # Spark (a preflight sub-operation) and the wait's text drops its evidence
    # for a moment.  Neither is progress, so the window keeps running.
    def checking(detail: str, operation_phase: str):
        child = {
            "phase": "runtime-install",
            "node_ids": [],
            "retry_attempt": 4,
            "operation": {
                "phase": operation_phase,
                "completed_bytes": 0,
                "total_bytes_known": False,
                "last_progress_at": "2026-10-14T02:00:00+00:00",
                "activity": "active",
                "members": [],
            },
        }
        return _waiting(detail, child_progress=child)

    first = _observe(None, checking("install.capacity_busy: disk", "prepare"), START)
    for minute, (detail, phase) in enumerate(
        [
            ("install.capacity_busy", "runtime-preflight"),
            ("install.capacity_busy: disk and stale inventory", "prepare"),
        ],
        start=1,
    ):
        again = _observe(
            first, checking(detail, phase), START + timedelta(minutes=minute)
        )
        assert again is not None and again.first_seen_at == START


def test_no_wait_or_an_ended_load_carries_no_stall() -> None:
    stalled = _repeat(9)
    idle = FleetProfileApplicationProgress()
    assert _observe(stalled, idle, START + timedelta(hours=1)) is None
    ended = _observe(stalled, _waiting(), START + timedelta(hours=1), state="cancelled")
    assert ended is None


def test_the_alarm_itself_is_not_part_of_the_wait() -> None:
    stall = _repeat(BURST)
    assert stall is not None
    shown = _waiting(attempt=BURST + 2)
    shown.blockers.append(stalled_blocker(stall))
    after = _observe(stall, shown, START + timedelta(hours=1))
    assert after is not None and after.first_seen_at == stall.first_seen_at


_NODE = "spk_" + "0a1b2c3d" * 4


def _persisted_wait(code: WaitReason, node: str = _NODE):
    """A wait recorded only as the load's persisted wait, not in its progress."""

    return [make_blocker(code, f"Awaiting the exact receipt ({code})", node_ids=[node])]


def _wait_only(previous, progress, waiting, at, *, before):
    return next_stall(
        previous,
        progress,
        before=before,
        state=LifecycleState.RUNNING.value,
        current_step=0,
        current_operation_id="op-1",
        observed_at=at,
        waiting=waiting,
    )


@pytest.mark.parametrize(
    "code", [WaitReason.RECEIPT_MISSING, WaitReason.STOP_UNCONFIRMED]
)
def test_a_wait_held_only_in_the_persisted_wait_is_an_alarm_after_a_window(
    code: WaitReason,
) -> None:
    # The load's progress names no blocker: its only wait is the persisted one
    # (a missing child receipt, an unconfirmed stop).  It was never observed.
    idle = FleetProfileApplicationProgress(attempt=1)
    waiting = _persisted_wait(code)
    stall = None
    for minute in range(40):
        stall = _wait_only(
            stall, idle, waiting, START + timedelta(minutes=minute), before=idle
        )
    assert stall is not None and stall.first_seen_at == START
    assert stall.detail.startswith(f"{code}: ")
    assert not is_stalled(
        stall, now=START + PROGRESS_STALL_WINDOW - timedelta(seconds=1)
    )
    assert is_stalled(stall, now=START + PROGRESS_STALL_WINDOW)
    # The alarm itself in the persisted wait is not part of what it waits on.
    shown = [*waiting, stalled_blocker(stall)]
    again = _wait_only(stall, idle, shown, START + timedelta(hours=1), before=idle)
    assert again == stall


def test_movement_under_a_persisted_wait_keeps_it_from_the_alarm() -> None:
    # The child copies bytes every ten minutes while the load waits for its
    # receipt: it waits for hours and is never stalled.
    waiting = _persisted_wait(WaitReason.RECEIPT_MISSING)
    before = FleetProfileApplicationProgress(attempt=1)
    stall = None
    for minute in range(1, 4 * 60):
        at = START + timedelta(minutes=minute)
        copied = minute // 10 * 10
        progress = _waiting(
            blockers=[],
            child_progress={"phase": "target-copy", "bytes": copied, "node_ids": []},
        )
        stall = _wait_only(stall, progress, waiting, at, before=before)
        before = progress
        assert not is_stalled(stall, now=at + timedelta(seconds=59)), minute
    # A different Spark in the persisted wait is a different wait: it starts over.
    later = START + timedelta(hours=5)
    moved = _wait_only(
        stall,
        before,
        _persisted_wait(WaitReason.RECEIPT_MISSING, "spk_" + "1" * 32),
        later,
        before=before,
    )
    assert moved is not None and moved.first_seen_at == later
    # The wait ends: nothing is left to be stalled.
    assert _wait_only(moved, before, [], later, before=before) is None


def test_every_process_with_the_fleet_profile_service_counts_observations() -> None:
    # The flush hook registers with its module; the service package must load it
    # (the worker imports the service, not this module).
    probe = (
        "import sys, vonk_control.fleet_profiles\n"
        "assert 'vonk_control.progress_stall' in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, timeout=60)
