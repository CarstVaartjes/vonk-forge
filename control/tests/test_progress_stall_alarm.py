"""A load whose wait does not move for a stall window is seen as stalled, and
stops being so on progress.

The alarm is informative: the load keeps its intent and keeps waiting, the
profile and the platform summary name it, and the first movement clears it.
Retrying the wait is not movement: a preparation or a build that advances keeps
its load out of the alarm however long it takes.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    LifecycleState,
    ObservedAssignmentState,
    ProfileReasonCode,
    RecipeImageCode,
)
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.models import (
    FleetProfileApplication,
    NodeInventorySnapshot,
    ResourceReservation,
)
from vonk_control.operation_blockers import ProgressAlarmCode, make_blocker
from vonk_control.platform_observation import PlatformObserver
from vonk_control.progress_stall import PROGRESS_STALL_WINDOW

from .test_attempt_residues import _load, _loop, _switch
from .test_fleet_profile_continuity import _world
from .test_fleet_profiles import NOW, _database, _input, _node_id, _seed, _uuid
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


def _nothing_moves_for_the_window(load) -> None:
    """Only the clock advances: just short of a window it is still only a wait."""

    stall = _application(load).progress.stall
    assert stall is not None
    load.now[0] = stall.first_seen_at + PROGRESS_STALL_WINDOW - timedelta(seconds=1)
    assert not _alarm(load) and not _platform_stalled(load)
    load.now[0] += timedelta(seconds=1)


def _sparks_report(load, free=None) -> None:
    """Every Spark reports its inventory now (with ``free`` disk, if given)."""

    with load.sessions.begin() as session:
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            if free is not None:
                snapshot.disk_free_bytes = free[snapshot.node_id]
            snapshot.observed_at = snapshot.received_at = load.now[0]


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

    def waiting() -> bool:
        progress = _application(load).progress
        return (
            progress.stall is not None
            and progress.stall.repeats >= 2
            and progress.child_progress is not None
            and progress.child_progress.operation is not None
        )

    for _ in range(20):
        if waiting():
            break
        _loop(load, lambda: False, rounds=1)
        _follow_due(load)
    assert waiting(), _application(load).status_reason

    # The load re-checks its child more often than the child retries: those
    # re-checks rewrite its progress but never move the wait.
    application = _application(load)
    stall = application.progress.stall
    child_attempt = application.progress.child_progress.retry_attempt
    checks = set()
    for _ in range(6):
        _follow_due(load)
        load.profiles.tick()
        rechecked = _application(load)
        checks.add(rechecked.progress.retry_due_at)
        assert rechecked.progress.child_progress.retry_attempt == child_attempt
        assert rechecked.progress.stall == stall
    assert len(checks) > 1

    _nothing_moves_for_the_window(load)
    application = _application(load)
    # Still waiting, never cancelled: the alarm sits after the wait it repeats.
    assert application.state == "running"
    (alarm,) = _alarm(load)
    assert application.blockers[0].code != ProgressAlarmCode.STALLED
    assert application.progress.stall is not None
    assert application.progress.stall.detail in alarm.detail
    (listed,) = _platform_stalled(load)
    assert listed.application_id == load.application.id
    assert listed.first_seen_at == application.progress.stall.first_seen_at

    _sparks_report(load, free)  # the disk is freed
    # The next preflight sees the freed disk and the install goes ahead.
    _loop(load, lambda: not _alarm(load), rounds=20)
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

    def waiting() -> bool:
        stall = _application(load).progress.stall
        return stall is not None and stall.repeats >= 2

    for _ in range(20):
        if waiting():
            break
        load.profiles.tick()
        _follow_due(load)
    assert waiting(), _application(load).status_reason

    _nothing_moves_for_the_window(load)
    application = _application(load)
    assert application.state in {"running", "queued"}
    (alarm,) = _alarm(load)
    assert "recipe start payload observation is unavailable" in alarm.detail
    assert application.blockers[0].code != ProgressAlarmCode.STALLED
    (listed,) = _platform_stalled(load)
    assert listed.application_id == load.application.id

    monkeypatch.setattr(adapter, "advance", real_advance)
    _sparks_report(load)
    _loop(load, lambda: not _alarm(load), rounds=10)
    assert not _alarm(load) and not _platform_stalled(load)
    assert _settle(load), _application(load).status_reason


@pytest.mark.parametrize(
    "preparing", [RecipeImageCode.PREPARING, RecipeImageCode.BUILD_WAIT]
)
def test_a_load_waiting_on_a_working_preparation_is_never_stalled(
    preparing: RecipeImageCode,
) -> None:
    """Catches the 10-15 false alarm: a load waiting while the Controller
    downloads its model or builds its image retries every minute or so, and
    was called stalled after five of those retries.  It waits as long as the
    preparation advances, and is stalled only a whole window after it stops."""

    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    now = [NOW]
    advanced = [NOW]

    def _missing(_session, _assignment, _node_ids, **_kwargs):
        raise ValueError("no successful immutable runtime image build is available")

    def _starter(
        recipe_revision_id: str, *, actor: str, application_id: str | None = None
    ):
        # Still preparing, whether it advanced or not: only its progress says.
        return [make_blocker(preparing, "Preparing the model and runtime image.")]

    def _progressed(recipe_revision_id: str, *, application_id: str | None = None):
        return advanced[0]

    service = FleetProfileService(
        sessions, clock=lambda: now[0], assessment_provider=_missing
    )
    service.bind_preparation_starter(_starter, progress=_progressed)
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(profile.id, request_key=_uuid(44), actor="admin")

    def stalled():
        return [
            item
            for item in service.application(application.id).blockers
            if item.code == ProgressAlarmCode.STALLED
        ]

    def listed():
        observer = PlatformObserver(sessions, clock=lambda: now[0])
        return observer.read().stalled_applications

    def retry_for(span: timedelta) -> None:
        end = now[0] + span
        while now[0] < end:
            now[0] += timedelta(minutes=2)
            service._observe_pending_admissions(now[0])
            assert service.application(application.id).state == "queued"

    # Two hours of a download or build that reports an advance only every ten
    # minutes (a long build step says nothing in between).
    for _ in range(12):
        retry_for(timedelta(minutes=10))
        assert not stalled() and not listed()
        advanced[0] = now[0]

    # It stops advancing but still says it prepares: a whole window later the
    # load is shown stalled, still waiting.
    retry_for(PROGRESS_STALL_WINDOW + timedelta(minutes=2))
    (alarm,) = stalled()
    stall = service.application(application.id).progress.stall
    assert stall is not None and stall.detail in alarm.detail
    (row,) = listed()
    assert row.application_id == application.id

    # The first advance clears it.
    advanced[0] = now[0]
    retry_for(timedelta(minutes=2))
    assert not stalled() and not listed()


def test_a_backoff_declined_alike_on_every_retry_is_stalled() -> None:
    """Production 2026-10-16: a load backed off for five hours, every retry
    declined with profile.retry_conflict, while the platform listed no stalled
    load. It keeps backing off (a wait is not a failure) and is shown stalled
    a whole window after it last moved; a different wait starts over."""
    sessions, service, _adapter, first, _original, _both = _world()
    now = [NOW + timedelta(minutes=1)]
    service._clock = lambda: now[0]
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, first.id)
        assert row is not None
        row.state = "backoff"  # its child failed retryably

    def decline(code=ProfileReasonCode.RETRY_CONFLICT) -> None:
        now[0] += timedelta(minutes=2)
        service._decline_retry_application(
            first.id, code, "Recovery would stop an unreviewed workload"
        )

    def listed():
        return (
            PlatformObserver(sessions, clock=lambda: now[0]).read().stalled_applications
        )

    decline()
    stall = service.application(first.id).progress.stall
    assert stall is not None
    while now[0] < stall.first_seen_at + PROGRESS_STALL_WINDOW - timedelta(minutes=2):
        decline()
        assert not listed()
    decline()
    decline()
    application = service.application(first.id)
    assert application.state == "backoff"
    assert any(item.code == ProgressAlarmCode.STALLED for item in application.blockers)
    (row,) = listed()
    assert row.application_id == first.id and row.state == "backoff"
    assert row.detail.startswith(f"{ProfileReasonCode.RETRY_CONFLICT}: ")
    assert row.first_seen_at == stall.first_seen_at and row.repeats > 2

    decline(ProfileReasonCode.RETRY_REVIEW_UNAVAILABLE)
    assert not listed()


def test_a_load_waiting_for_the_load_it_continues_is_never_silent(monkeypatch) -> None:
    """A newer load that continues an older load's assignments waits for it
    to finish them. That wait recorded nothing, so it could last for hours
    unseen; it is now a typed wait, shown stalled a whole window after it
    last moved, and the load keeps waiting."""
    sessions, service, adapter, first, _original, both = _world()
    now = [NOW + timedelta(minutes=1)]
    service._clock = lambda: now[0]
    held = service.application(first.id).current_operation_id
    assert held is not None
    real_advance = adapter.advance

    def advance(operation_id, *, session=None):
        if operation_id == held:
            return adapter.get(operation_id)  # the older load's child is slow
        return real_advance(operation_id, session=session)

    adapter.advance = advance
    newer = service.apply(both.id, request_key=_uuid(18030), actor="admin")

    def waiting():
        blockers = service.application(newer.id).progress.blockers
        return [
            b
            for b in blockers
            if b.code == ProfileReasonCode.CONTINUING_ASSIGNMENTS_PENDING
        ]

    def listed():
        return (
            PlatformObserver(sessions, clock=lambda: now[0]).read().stalled_applications
        )

    for _ in range(20):
        if waiting():
            break
        now[0] += timedelta(minutes=1)
        service.tick()
    (wait,) = waiting()
    assert first.id in wait.detail and wait.node_ids == [_node_id(1)]
    stall = service.application(newer.id).progress.stall
    assert stall is not None and not listed()

    now[0] = stall.first_seen_at + PROGRESS_STALL_WINDOW
    service.tick()
    (row,) = listed()
    assert row.application_id == newer.id
    assert row.detail.startswith(
        f"{ProfileReasonCode.CONTINUING_ASSIGNMENTS_PENDING}: "
    )

    assert service.application(newer.id).state == "running"  # waiting, not ended

    # Restoring the exact child receipt releases the dependent load through
    # normal ticks. The escalation is informational, never an operator latch.
    adapter.advance = real_advance
    real_state = FleetProfileService._assignment_state.__func__

    def observed_running(cls, session, assignment, **kwargs):
        state = real_state(cls, session, assignment, **kwargs)
        # The synthetic executor's matching workload observation arrives with
        # its receipt. Production reads these facts from its run/installation.
        if adapter.get(held).state == LifecycleState.SUCCEEDED:
            state.current_state = ObservedAssignmentState.RUNNING
        return state

    monkeypatch.setattr(
        FleetProfileService, "_assignment_state", classmethod(observed_running)
    )
    for _ in range(24):
        now[0] += timedelta(seconds=30)
        service.tick()
        if service.application(newer.id).state == LifecycleState.SUCCEEDED:
            break
    else:
        pytest.fail(service.application(newer.id).model_dump_json())
    assert not waiting()
    assert not listed()
