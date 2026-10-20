"""A load the Controller retries by itself backs off; ``failed`` is final.

A failure that will be retried is a wait, not an end: the load is stored
``backoff`` with its last failure as its reason and its next attempt time, and
the retry, a newer load or a cancel moves it on as a live load.  ``failed`` is
written only when nothing will try the load again, and nothing rewrites it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState
from vonk_control.lifecycle.core import RECOVERY
from vonk_control.models import FleetProfileApplication, Job

from .test_fleet_profile_recovery_current import (
    _crash_rounds,
    _failed_profile,
)
from .test_fleet_profiles import _uuid

_CAUSE = "temporary runtime dependency unavailable"


def _stored(sessions, application_id: str) -> FleetProfileApplication:
    with sessions() as session:
        row = session.get(FleetProfileApplication, application_id)
        assert row is not None
        session.expunge(row)
        return row


def _others(sessions, application_id: str) -> tuple[FleetProfileApplication, ...]:
    with sessions() as session:
        return tuple(
            session.scalars(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.id != application_id
                )
            )
        )


def _complete_switch_children(sessions, *, besides: str) -> int:
    """The agent finishes every Run/Switch child except ``besides``."""

    completed = 0
    with sessions.begin() as session:
        for child in session.scalars(
            select(Job).where(Job.kind == "recipe.run-switch.v2", Job.id != besides)
        ):
            if child.state in {"succeeded", "failed", "cancelled"}:
                continue
            phases = child.payload["plan"]["phases"]
            child.result = {
                **child.result,
                "phase_index": len(phases),
                "completed_phases": [phase["kind"] for phase in phases],
                "child_operation_id": None,
            }
            child.state = "succeeded"
            completed += 1
    return completed


def test_a_retryable_failure_backs_off_with_its_evidence_and_its_retry_succeeds(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, service, _profile, _desired, first, first_child, _nodes = (
        _failed_profile(tmp_path)
    )
    waiting = service.application(first.id)
    assert waiting.state == "backoff"
    assert _CAUSE in (waiting.status_reason or "")
    assert waiting.next_attempt_at is not None
    assert _stored(sessions, first.id).state == "backoff"

    later = lifecycle._clock() + timedelta(hours=2)
    service._clock = lambda: later
    for _ in range(6):
        service.tick()
        if _others(sessions, first.id):
            break
    (retry,) = _others(sessions, first.id)
    ended = service.application(first.id)
    assert ended.state == "superseded" and ended.superseded_by == retry.id
    assert _CAUSE in (ended.status_reason or "")

    for _ in range(6):
        service.tick()
        _complete_switch_children(sessions, besides=first_child)
        if service.application(retry.id).state == "succeeded":
            break
    assert service.application(retry.id).state == "succeeded"


def test_repeated_failure_recovers_without_a_new_load(tmp_path: Path) -> None:
    sessions, lifecycle, service, _profile, _desired, _first, first_child, _nodes = (
        _failed_profile(tmp_path)
    )
    clock = {"now": lifecycle._clock()}
    service._clock = lambda: clock["now"]
    cause = "runtime exited at start"
    rounds = [cause] * (RECOVERY.max_failures + 2)
    applications = _crash_rounds(sessions, service, clock, rounds)
    assert applications > RECOVERY.max_failures + 1
    with sessions() as session:
        last_id = session.scalar(
            select(FleetProfileApplication.id)
            .order_by(FleetProfileApplication.created_at.desc())
            .limit(1)
        )
    assert last_id is not None
    waiting = service.application(last_id)
    assert waiting.state == LifecycleState.BACKOFF
    assert waiting.next_attempt_at is not None
    assert cause in (waiting.status_reason or "")
    # The runtime fault clears. The standing owner retries the same intent;
    # there is no second apply or explicit retry request.
    for _ in range(12):
        clock["now"] += timedelta(hours=2)
        service.tick()
        _complete_switch_children(sessions, besides=first_child)
        with sessions() as session:
            succeeded = session.scalar(
                select(FleetProfileApplication.id).where(
                    FleetProfileApplication.state == LifecycleState.SUCCEEDED
                )
            )
        if succeeded is not None:
            break
    assert succeeded is not None
    assert service.application(last_id).state == LifecycleState.SUPERSEDED


def test_a_newer_load_supersedes_a_backing_off_load(tmp_path: Path) -> None:
    _sessions, _lifecycle, service, profile, _desired, first, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    assert service.application(first.id).state == "backoff"

    fresh = service.apply(profile.id, request_key=_uuid(19501), actor="admin")

    assert fresh.id != first.id
    ended = service.application(first.id)
    assert ended.state == "superseded"
    assert ended.superseded_by == fresh.id
    assert not service.retry_eligible(first.id)


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_failure_an_older_controller_kept_retrying_is_adopted_as_backoff(
    tmp_path: Path,
) -> None:
    """Upgrade: an older Controller stored a failure it retried as ``failed``
    (presented queued).  It is adopted once as the backoff it was, and retried."""

    sessions, lifecycle, service, _profile, _desired, first, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, first.id)
        assert row is not None
        row.state = "failed"
        row.progress = {**row.progress, "retry_due_at": None}

    clock = {"now": lifecycle._clock() + timedelta(hours=2)}
    service._clock = lambda: clock["now"]
    service.tick()
    adopted = service.application(first.id)
    assert adopted.state == "backoff"
    assert _CAUSE in (adopted.status_reason or "")
    assert adopted.next_attempt_at is not None

    for _ in range(6):
        clock["now"] += timedelta(hours=2)
        service.tick()
        if _others(sessions, first.id):
            break
    (retry,) = _others(sessions, first.id)
    assert service.application(first.id).superseded_by == retry.id


def test_a_failure_that_is_not_retried_keeps_its_recorded_end(
    tmp_path: Path,
) -> None:
    """The adoption is for retried failures only: a definite one stays exactly
    as it was recorded."""

    crash = "AssertionError: the attention heads do not divide the tensor size"
    sessions, lifecycle, service, _profile, _desired, ended, _child, _nodes = (
        _failed_profile(tmp_path, crash=crash)
    )
    recorded = _stored(sessions, ended.id)
    assert recorded.state == "failed"
    later = lifecycle._clock() + timedelta(hours=2)
    service._clock = lambda: later
    for _ in range(4):
        service.tick()
    again = _stored(sessions, ended.id)
    assert (again.state, again.status_reason, again.progress, again.updated_at) == (
        recorded.state,
        recorded.status_reason,
        recorded.progress,
        recorded.updated_at,
    )
