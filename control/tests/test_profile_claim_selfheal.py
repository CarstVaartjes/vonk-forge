"""A live profile load never loops on a capacity claim that went missing.

An application that fails releases its unassigned claims; when its live child
is resumed the same application takes them back. A disk claim is also only
bookkeeping for a promise, not a precondition of the work: when its row is gone
(never reserved, or released together with the installation that held it) or
has drifted from the accepted plan, the live load reserves again under the
ordinary admission locks and capacity check. Without room it waits with a named
reason that the profile and the Fleet both show, and resumes once room returns.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState, ReservationState, RunSwitchCode
from vonk_control.fleet_projection import FleetProjection
from vonk_control.models import (
    FleetProfileApplication,
    Job,
    NodeInventorySnapshot,
    RecipeInstallation,
    ResourceReservation,
)

from .profile_due_fixtures import next_profile_due
from .test_attempt_residues import _at_phase, _load, _loop, _switch


def _settle(load, *, rounds: int = 40) -> bool:
    """Run to success at the actual persisted child and parent retry times."""

    for _ in range(rounds):
        if load.profiles.application(load.application.id).state == "succeeded":
            return True
        _loop(load, lambda: False, rounds=1)
        _follow_due(load)
    return load.profiles.application(load.application.id).state == "succeeded"


def _the_switch(load) -> Job:
    switch = _switch(load)
    assert switch is not None
    return switch


def _follow_due(load) -> None:
    """Advance the shared clock to the next actual persisted retry fence."""

    due = next_profile_due(load.profiles, load.planner, load.application.id)
    if due is not None:
        assert due > load.now[0]
        load.now[0] = due


def _application(load):
    return load.profiles.application(load.application.id)


def _claims(load, *, owner_kind: str, kind: str | None = None):
    with load.sessions() as session:
        return tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == owner_kind,
                    *([ResourceReservation.kind == kind] if kind else []),
                )
            )
        )


def _installed(load) -> list[RecipeInstallation]:
    with load.sessions() as session:
        return [
            row
            for row in session.scalars(select(RecipeInstallation))
            if row.state != "uninstalled"
        ]


@pytest.mark.parametrize("damage", ["absent", "released", "plan-drift", "amount-drift"])
def test_a_load_reserves_disk_again_when_its_claim_is_gone_or_stale(
    tmp_path: Path, damage: str
) -> None:
    load = _load(tmp_path)
    assert _loop(load, lambda: _switch(load) is not None, complete=False, rounds=3)
    with load.sessions.begin() as session:
        claims = list(
            session.scalars(
                select(ResourceReservation).where(ResourceReservation.kind == "disk")
            )
        )
        assert len(claims) == 2
        for claim in claims:
            if damage == "absent":
                session.delete(claim)
            elif damage == "released":
                claim.state = "released"
            elif damage == "plan-drift":
                claim.plan_digest = "0" * 64
            else:
                claim.amount_bytes += 1
    assert _settle(load), _application(load).status_reason
    (installation,) = _installed(load)
    assert installation.state == "installed"


def test_a_transient_child_observation_retains_claims_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost child observation preserves ownership until exact effects settle."""

    load = _load(tmp_path)
    assert _loop(load, lambda: _switch(load) is not None, complete=False, rounds=3)
    adapter = load.profiles._switch_adapter
    real_advance = adapter.advance
    faults: list[str] = []

    def advance_once_flaky(operation_id, *, session):
        if not faults:
            faults.append(operation_id)
            raise RuntimeError("Run/Switch child is unavailable")
        return real_advance(operation_id, session=session)

    monkeypatch.setattr(adapter, "advance", advance_once_flaky)
    load.profiles.tick()
    with load.sessions() as session:
        row = session.get(FleetProfileApplication, load.application.id)
        assert faults and row is not None and row.state == LifecycleState.RUNNING
        assert row.current_operation_id == faults[0]
    claims = _claims(load, owner_kind="fleet-profile", kind="disk")
    assert claims and all(claim.state == ReservationState.ACTIVE for claim in claims)
    # Unknown observations retain claims and the exact child. Recovery follows
    # its persisted due time, without an operator retry or replacement receipt.
    _follow_due(load)
    assert _settle(load), _application(load).status_reason
    (installation,) = _installed(load)
    assert installation.state == "installed"


def test_a_load_survives_the_release_of_the_installation_that_held_its_claim(
    tmp_path: Path,
) -> None:
    """Cleaning up an earlier attempt's plan frees the claim it was handed."""

    load = _load(tmp_path)
    assert _loop(load, lambda: _at_phase(load, "target-copy"), complete=False)
    (held,) = {claim.owner_id for claim in _claims(load, owner_kind="installation")}
    load.lifecycle.abandon_never_installed(held)
    assert all(
        claim.state == "released" for claim in _claims(load, owner_kind="installation")
    )
    # A retry from the first phase (as any replanned attempt does).
    with load.sessions.begin() as session:
        job = session.get(Job, _the_switch(load).id)
        assert job is not None and isinstance(job.result, dict)
        job.result = {**job.result, "phase_index": 0, "item_index": 0}
    assert _settle(load), _application(load).status_reason
    (installation,) = _installed(load)
    assert installation.id != held and installation.state == "installed"


def test_active_claims_with_different_owners_are_named_not_adopted(
    tmp_path: Path,
) -> None:
    """A handoff is atomic: split ownership is a fault, never silently replaced."""

    load = _load(tmp_path)
    assert _loop(load, lambda: _at_phase(load, "target-copy"), complete=False)
    with load.sessions.begin() as session:
        claim = session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "installation",
                ResourceReservation.kind == "disk",
            )
        ).first()
        assert claim is not None
        claim.owner_kind = "fleet-profile"
        claim.owner_id = load.application.id
        job = session.get(Job, _the_switch(load).id)
        assert job is not None and isinstance(job.result, dict)
        job.result = {**job.result, "phase_index": 0, "item_index": 0}
    with load.sessions() as session:
        owners = {
            row.id: (row.owner_kind, row.owner_id)
            for row in session.scalars(
                select(ResourceReservation).where(ResourceReservation.kind == "disk")
            )
        }
    for _ in range(20):
        _loop(load, lambda: False, rounds=1)
        _follow_due(load)
        if RunSwitchCode.INSTALLATION_HANDOFF_INCONSISTENT in (
            _the_switch(load).status_reason or ""
        ):
            break
    assert RunSwitchCode.INSTALLATION_HANDOFF_INCONSISTENT in (
        _the_switch(load).status_reason or ""
    )
    assert _application(load).state == "running"
    with load.sessions.begin() as session:
        claims = list(
            session.scalars(
                select(ResourceReservation).where(ResourceReservation.kind == "disk")
            )
        )
        assert {row.id: (row.owner_kind, row.owner_id) for row in claims} == owners
        installation_owner = next(
            row.owner_id for row in claims if row.owner_kind == "installation"
        )
        for row in claims:
            row.owner_kind = "installation"
            row.owner_id = installation_owner
    assert _settle(load), _application(load).status_reason


def test_without_room_a_lost_claim_waits_visibly_and_resumes(tmp_path: Path) -> None:
    load = _load(tmp_path)
    fleet = FleetProjection(load.sessions, clock=load.lifecycle._clock)
    assert _loop(load, lambda: _switch(load) is not None, complete=False, rounds=3)
    with load.sessions.begin() as session:
        for claim in session.scalars(
            select(ResourceReservation).where(ResourceReservation.kind == "disk")
        ):
            session.delete(claim)
        snapshots = list(session.scalars(select(NodeInventorySnapshot)))
        free = {row.node_id: row.disk_free_bytes for row in snapshots}
        for snapshot in snapshots:
            snapshot.disk_free_bytes = 100

    def stalled() -> bool:
        return any(blocker.code == "run-switch.phase-retry" for blocker in blockers())

    def blockers():
        return _application(load).blockers

    for _ in range(12):
        _loop(load, lambda: False, rounds=1)
        _follow_due(load)
        if stalled():
            break
    application = _application(load)
    # Waiting, not failed, and the profile itself names what it keeps retrying.
    assert application.state == "running"
    assert stalled()
    assert application.status_reason
    reasons = {warning.code for node in fleet.read().nodes for warning in node.warnings}
    assert "profile.retrying" in reasons

    with load.sessions.begin() as session:
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.disk_free_bytes = free[snapshot.node_id]
    assert _settle(load), _application(load).status_reason
    assert not _application(load).blockers
    assert "profile.retrying" not in {
        warning.code for node in fleet.read().nodes for warning in node.warnings
    }
