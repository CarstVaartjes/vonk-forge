"""A load whose wait does not move for a stall window is seen as stalled, and
stops being so on progress.

The alarm is informative: the load keeps its intent and keeps waiting, the
profile and the platform summary name it, and the first movement clears it.
Retrying the wait is not movement: a preparation or a build that advances keeps
its load out of the alarm however long it takes.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from vonk_agent_protocol import (
    AdmissionCode,
    AgentFailureKind,
    AgentOperation,
    LifecycleState,
    ObservedAssignmentState,
    ProfileReasonCode,
    RecipeImageCode,
    WaitOwnerKind,
    WaitReason,
    WaitResumeCondition,
)
from vonk_agent_protocol.agent_words import (
    ProfileChildPhase,
    ProfileReasonSeverity,
    ProgressPhase,
)
from vonk_control.fleet_profile_contract import FleetProfileApplicationProgress
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.models import (
    FleetProfileApplication,
    NodeInventorySnapshot,
    ResourceReservation,
)
from vonk_control.operation_blockers import (
    BlockingOperation,
    ProgressAlarmCode,
    make_blocker,
)
from vonk_control.persisted_waits import read_wait, schedule_wait, write_wait
from vonk_control.platform_observation import PlatformObserver
from vonk_control.progress_stall import PROGRESS_STALL_WINDOW

from .test_attempt_residues import _load, _loop, _switch
from .test_fleet_profile_continuity import _world
from .test_fleet_profiles import NOW, _database, _input, _node_id, _seed, _uuid
from .test_profile_claim_selfheal import _application, _follow_due, _settle
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _ChildView,
    _fixture_child_result,
)


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
        checks.add(rechecked.next_attempt_at)
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


def _stored(load):
    """The application row's own progress and persisted wait, as stored."""

    with load.sessions() as session:
        row = session.get(FleetProfileApplication, load.application.id)
        assert row is not None
        progress = FleetProfileApplicationProgress.model_validate_json(
            json.dumps(row.progress)
        )
        return progress, read_wait(row)


def test_a_load_waiting_only_on_a_missing_child_receipt_is_stalled_with_the_worker_down(
    tmp_path: Path,
) -> None:
    """Catches the silent receipt wait: the Run/Switch child's agent work is
    never delivered (as when a lane Spark goes away while still enrolled), and
    the load records that wait only as its persisted wait, never in its
    progress blockers.  It carried no stall, so it waited unseen forever.  It is
    now stalled one window after it last moved, decided when it is read with
    no worker running, and the delivered work clears it."""

    load = _load(tmp_path)
    assert _loop(load, lambda: _switch(load) is not None, complete=False, rounds=3)
    for _ in range(4):  # the child's own wait settles into one dependency
        _loop(load, lambda: False, complete=False, rounds=1)
    progress, wait = _stored(load)
    assert wait is not None and wait.reason is WaitReason.RECEIPT_MISSING
    assert not progress.blockers  # the wait lives only in the persisted wait
    stall = progress.stall
    assert stall is not None and stall.first_seen_at >= wait.first_blocked_at

    # The worker is down: from here only the clock moves until the alarm shows.
    _nothing_moves_for_the_window(load)
    application = _application(load)
    assert application.state == LifecycleState.RUNNING.value  # still waiting
    assert application.blockers[0].code == WaitReason.RECEIPT_MISSING
    (alarm,) = _alarm(load)
    assert stall.detail in alarm.detail
    # Its shape: appended last, after the wait it repeats, to the view's
    # progress blockers, its wait's blockers and its blockers alike; the row
    # stores only the stall, never the alarm.
    assert alarm.severity == ProfileReasonSeverity.ERROR.value
    assert application.wait is not None
    for shown in (
        application.blockers,
        application.progress.blockers,
        application.wait.blockers,
    ):
        assert [item.code for item in shown] == [
            WaitReason.RECEIPT_MISSING,
            ProgressAlarmCode.STALLED,
        ]
    assert not _stored(load)[0].blockers
    (listed,) = _platform_stalled(load)
    assert listed.application_id == load.application.id

    # The Spark is back and its work is delivered: the alarm clears and the
    # load succeeds.
    _sparks_report(load)
    _loop(load, lambda: not _alarm(load), rounds=20)
    assert not _alarm(load) and not _platform_stalled(load)
    assert _settle(load), _application(load).status_reason


def test_a_load_already_waiting_unseen_at_upgrade_is_observed_by_its_next_check(
    tmp_path: Path,
) -> None:
    """A row the previous release wrote: its receipt wait is persisted but it
    carries no stall.  The next re-check rewrites the same wait and moves only
    its due time (neither its evidence nor its progress changes); that check
    observes it, and a window later it is stalled."""

    load = _load(tmp_path)
    assert _loop(load, lambda: _switch(load) is not None, complete=False, rounds=3)
    for _ in range(4):
        _loop(load, lambda: False, complete=False, rounds=1)
    progress, wait = _stored(load)
    assert wait is not None and wait.reason is WaitReason.RECEIPT_MISSING
    stored = progress.model_dump(mode="json", exclude={"stall"})
    with load.sessions.begin() as session:  # written below the flush hook
        session.execute(
            update(FleetProfileApplication)
            .where(FleetProfileApplication.id == load.application.id)
            .values(progress=stored)
        )
    assert _stored(load)[0].stall is None

    with load.sessions.begin() as session:  # the reconciler's re-check
        row = session.get(FleetProfileApplication, load.application.id)
        assert row is not None and row.next_action_at is not None
        schedule_wait(row, row.next_action_at + timedelta(seconds=2), now=load.now[0])
    progress, after = _stored(load)
    assert after is not None and after.reason is WaitReason.RECEIPT_MISSING
    assert progress.stall is not None
    _nothing_moves_for_the_window(load)
    assert _alarm(load) and _platform_stalled(load)


def test_an_unconfirmed_stop_written_only_as_the_wait_is_stalled_then_cleared(
    tmp_path: Path,
) -> None:
    """The superseded-workload stop wait (``switch_observation``) is written
    with ``write_wait`` alone and without touching the row's time: it starts
    its own stall when it is written, is an alarm one window later, and a
    cleared wait drops it."""

    load = _load(tmp_path)
    assert _loop(load, lambda: _switch(load) is not None, complete=False, rounds=3)
    load.now[0] += timedelta(hours=1)  # the row's time is an hour old
    node = min(load.nodes)
    with load.sessions.begin() as session:
        row = session.get(FleetProfileApplication, load.application.id)
        assert row is not None
        operation = BlockingOperation(
            id="superseded-start-1", kind=AgentOperation.RECIPE_START.value
        )
        write_wait(
            row,
            identity=BlockingOperation(
                id=row.id, kind=WaitOwnerKind.FLEET_PROFILE.value
            ),
            kind=WaitOwnerKind.FLEET_PROFILE,
            reason=WaitReason.STOP_UNCONFIRMED,
            now=load.now[0],
            next_check=load.now[0] + timedelta(minutes=1),
            blockers=[
                make_blocker(
                    WaitReason.STOP_UNCONFIRMED,
                    "Awaiting the exact superseded workload cancellation receipt",
                    node_ids=[node],
                ).model_copy(update={"operation": operation})
            ],
            dependency_deadline=load.now[0] + timedelta(hours=1),
            resume_condition=WaitResumeCondition.EFFECT_OBSERVED,
        )
    progress, wait = _stored(load)
    assert wait is not None and wait.reason is WaitReason.STOP_UNCONFIRMED
    assert not progress.blockers
    assert progress.stall is not None
    assert progress.stall.first_seen_at == load.now[0]  # not the row's old time
    assert progress.stall.detail.startswith(f"{WaitReason.STOP_UNCONFIRMED}: ")
    assert not _alarm(load) and not _platform_stalled(load)

    _nothing_moves_for_the_window(load)
    (alarm,) = _alarm(load)
    assert WaitReason.STOP_UNCONFIRMED.value in alarm.detail
    (listed,) = _platform_stalled(load)
    assert listed.application_id == load.application.id

    with load.sessions.begin() as session:
        row = session.get(FleetProfileApplication, load.application.id)
        assert row is not None
        schedule_wait(row, None)
    progress, wait = _stored(load)
    assert wait is None and progress.stall is None
    assert not _alarm(load) and not _platform_stalled(load)


def _copying_load(tmp_path: Path):
    """A load whose Sparks need the model copied: its distribution child is
    driven by the test, reporting what an agent's copy reports."""

    transfer: list = []

    class Copy(RecordingArtifactExecutor):
        def execute(self, plan, phase, **kwargs):
            if phase.kind == ProfileChildPhase.TRANSFER:
                transfer[:] = [plan, phase]
            return super().execute(plan, phase, **kwargs)

    executor = Copy(child_transfer=True)
    load = _load(
        tmp_path,
        executor=executor,
        artifacts=CompleteArtifactInspector(
            reclaimable_bytes=30, missing_spark_bytes=1024
        ),
    )

    def report(
        child_id: str,
        copied: int,
        *,
        key: str | None = None,
        failed: bool = False,
        operation_phase: str = ProgressPhase.COPYING.value,
    ) -> None:
        """The child's execution (``key``, by default its id) copied ``copied``."""

        plan, phase = transfer
        payload: dict = {
            "progress": {"completed_bytes": copied, "total_bytes": 10**9},
            "operation": {
                "phase": operation_phase,
                "completed_bytes": copied,
                "total_bytes": 10**9,
                "total_bytes_known": True,
                "checkpoint": {"key": key or child_id, "sequence": copied},
                # The agent's sample of a new high (``sample_progress``).
                "last_progress_at": load.now[0].isoformat(),
                "members": [],
            },
        }
        if failed:
            payload |= {
                "failure_kind": AgentFailureKind.TEMPORARY_DEPENDENCY.value,
                "error_code": AdmissionCode.CAPACITY_BUSY.value,
                "reason": "The admission lock is held by another admission",
            }
        executor.children[child_id] = _ChildView(
            state=LifecycleState.FAILED.value
            if failed
            else LifecycleState.RUNNING.value,
            result=_fixture_child_result(plan, phase, payload),
        )

    return load, executor, report


def _running_children(executor) -> list[str]:
    return [
        child_id
        for child_id, child in executor.children.items()
        if child.state in {LifecycleState.QUEUED.value, LifecycleState.RUNNING.value}
    ]


def test_a_copy_retried_through_phase_retries_is_stalled_one_window_later(
    tmp_path: Path,
) -> None:
    """The 2026-10-24 incident through the real reconcile: the distribution
    child copies 300 bytes and fails retryably (admission.capacity_busy);
    each retry runs a runtime preflight and a new execution whose bytes start
    at zero and rise to 300 again.  The load shows a different wait at each
    step of the cycle (receipt-missing, run-switch.phase-retry, the preflight),
    and none of it is new after the first cycle: one window later it is
    stalled, while the cycle goes on."""

    load, executor, report = _copying_load(tmp_path)
    copied: dict[str, int] = {}
    places: set[str] = set()
    codes: set[str] = set()
    for _ in range(80):
        _loop(load, lambda: False, rounds=1)
        load.now[0] += timedelta(minutes=1)
        for child_id in _running_children(executor):
            copied[child_id] = copied.get(child_id, 0) + 100
            report(child_id, copied[child_id], failed=copied[child_id] >= 300)
        _sparks_report(load)
        application = _application(load)
        stall = application.progress.stall
        if stall is not None:
            places.add(stall.signature)
        codes.update(item.code for item in application.blockers)
        if _alarm(load):
            break
    assert _alarm(load), application.blockers
    assert application.state == LifecycleState.RUNNING.value  # still waiting
    assert len(places) == 1  # one place across the child's backoff and retry
    assert len(copied) > 3  # the child kept retrying the copy
    assert {WaitReason.RECEIPT_MISSING, ProgressAlarmCode.STALLED} <= codes
    # One window after the first cycle (a cycle takes about six minutes),
    # not eventually.
    assert load.now[0] <= application.created_at + PROGRESS_STALL_WINDOW + timedelta(
        minutes=10
    )
    assert stall is not None
    assert stall.first_seen_at + PROGRESS_STALL_WINDOW <= load.now[0]
    (listed,) = _platform_stalled(load)
    assert listed.application_id == load.application.id


def test_a_re_executed_copy_resumes_without_an_alarm(tmp_path: Path) -> None:
    """The agent restarts mid-copy (a parallel upgrade): the same execution
    runs again under a new checkpoint key, its bytes from zero, below what it
    had copied.  Climbing again is movement: no alarm while it climbs, and
    the load finishes."""

    load, executor, report = _copying_load(tmp_path)
    assert _loop(load, lambda: bool(_running_children(executor)), rounds=10)
    (child_id,) = _running_children(executor)
    key = child_id
    for copied in range(100, 600, 100):  # the first execution copies 500
        load.now[0] += timedelta(minutes=5)
        report(child_id, copied)
        _sparks_report(load)
        _loop(load, lambda: False, rounds=1)
    child = _application(load).progress.child_progress
    assert child is not None and child.retry_attempt is None  # one attempt
    for copied in range(10, 500, 10):  # the re-execution, ten bytes a step
        load.now[0] += timedelta(minutes=5)
        report(child_id, copied, key=f"{key}-restarted")
        _sparks_report(load)
        _loop(load, lambda: False, rounds=1)
        assert not _alarm(load), copied
    executor.children[child_id] = _ChildView(
        state=LifecycleState.SUCCEEDED.value, result=executor.children[child_id].result
    )
    _sparks_report(load)
    assert _settle(load), _application(load).status_reason


def test_a_load_moving_through_its_phases_for_longer_than_a_window_is_never_stalled(
    tmp_path: Path,
) -> None:
    """Every running load carries a persisted receipt wait (a load holds no
    lease), so only forward movement keeps it out of the alarm.  A load whose
    copy takes three quarters of an hour (a new high every five minutes) and
    whose child then goes through its install, start and verify phases and
    the agent's sub-phases within them is never shown stalled."""

    load, executor, report = _copying_load(tmp_path)
    shown: set[tuple[str, str | None]] = set()

    def observe() -> None:
        application = _application(load)
        child = application.progress.child_progress
        if child is not None:
            operation = child.operation
            shown.add((child.phase, None if operation is None else operation.phase))
        assert not _alarm(load), application.blockers

    assert _loop(load, lambda: bool(_running_children(executor)), rounds=10)
    (child_id,) = _running_children(executor)
    # The agent prepares the copy first, then copies: the step into a phase
    # of its own it has not shown yet is movement, before any byte moves.
    load.now[0] += timedelta(minutes=5)
    report(child_id, 0, operation_phase=ProgressPhase.PREPARING.value)
    _loop(load, lambda: False, rounds=1)
    observe()
    load.now[0] += PROGRESS_STALL_WINDOW - timedelta(minutes=8)
    _loop(load, lambda: False, rounds=1)
    observe()
    for copied in range(100, 1000, 100):  # 45 minutes of copying
        load.now[0] += timedelta(minutes=5)
        report(child_id, copied)
        _sparks_report(load)
        _loop(load, lambda: False, rounds=1)
        observe()
    executor.children[child_id] = _ChildView(
        state=LifecycleState.SUCCEEDED.value, result=executor.children[child_id].result
    )
    _sparks_report(load)
    for _ in range(40):
        if _application(load).state == LifecycleState.SUCCEEDED.value:
            break
        _loop(load, lambda: False, rounds=1)
        observe()
    application = _application(load)
    assert application.state == LifecycleState.SUCCEEDED.value, (
        application.status_reason
    )
    assert load.now[0] - application.created_at > PROGRESS_STALL_WINDOW
    assert len({phase for phase, _ in shown}) > 2 and len(shown) > 4, shown


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
    stalled = {row.application_id: row for row in listed()}
    row = stalled[newer.id]
    assert row.detail.startswith(
        f"{ProfileReasonCode.CONTINUING_ASSIGNMENTS_PENDING}: "
    )
    # The older load's child does not move either: its receipt wait is shown
    # stalled too, never silent.
    assert set(stalled) == {newer.id, first.id}

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
