"""Who owns a resource reservation, and whether that owner can still use it.

A reservation (disk, port, memory) is a promise that some operation is about to
write, bind or allocate. It is worth holding only while its owner can still do
that. The owner is *dead* when it no longer exists or has ended in a state that
cannot continue without asking again: a cancelled or failed profile application,
a stopped run, an uninstalled installation, an installation or build that no
live operation refers to any more. A dead owner's physical effects are what the
Spark's next inventory reports; its reservation is bookkeeping that would
otherwise block admission forever.

This module is the one predicate. The reconciler releases what it names dead,
and the admission blockers name the owners of what they count, from the same
decision, so an explanation and a release never disagree.

Kept on purpose:

* an installation that is ``installed``: its claim is not "dead", but once its
  Spark has reported its disk after the install completed the files are in the
  reported free space and the claim is released as covered
  (``release_covered_installation_claims``), unless a run of it is live;
* an installation whose last operation was cancelled or retired, or was a
  cleanup (uninstall, reconcile): its effect on the Spark is uncertain, so its
  claim stays until the exact cleanup proves the effect gone;
* anything a live operation still refers to, and any owner kind not listed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Text, cast, func, select
from sqlalchemy.orm import Session

from .fleet_profile_contract import FLEET_PROFILE_ENDED_STATES
from .inventory_repository import MAX_INVENTORY_FUTURE_SKEW
from .models import (
    STOPPABLE_RUN_STATES,
    FleetProfileApplication,
    InstallationNode,
    Job,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)

_TERMINAL_JOB_STATES = ("succeeded", "failed", "expired", "cancelled")
_ENDED_APPLICATION_STATES = FLEET_PROFILE_ENDED_STATES
# How long an unowned plan waits for the next attempt to adopt it.
ADOPTION_WINDOW = timedelta(minutes=15)
# A claim is created in the same transaction as the operation that owns it; this
# only absorbs a reader that sees an owner's state change before its successor.
OWNER_SETTLE_GRACE = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class ReservationOwner:
    kind: str
    owner_id: str
    state: str
    dead: bool
    noun: str
    detail: str = ""

    def describe(self) -> str:
        """``cancelled profile application 3f68e2f4``; a gone owner is ``missing``."""

        text = f"{self.state} {self.noun} {self.owner_id[:8]}"
        return f"{text} ({self.detail})" if self.detail else text


def _referencing_active_job(session: Session, owner_kind: str, owner_id: str) -> bool:
    """Whether a not-yet-ended operation was issued for, or records, this owner.

    An operation issued for the owner names it in its payload; a parent order
    (a Run/Switch that prepared an installation, or a profile) records the id in
    its progress. Either keeps the owner live.
    """

    return (
        session.scalar(
            select(Job.id)
            .where(
                Job.state.not_in(_TERMINAL_JOB_STATES),
                (
                    (Job.payload["owner_kind"].as_string() == owner_kind)
                    & (Job.payload["owner_id"].as_string() == owner_id)
                )
                | cast(Job.result, Text).contains(owner_id),
            )
            .limit(1)
        )
        is not None
    )


def run_has_live_operation(session: Session, run_id: str) -> bool:
    """Whether a not-yet-ended operation (a Start, a Stop) still owns this run."""

    return _referencing_active_job(session, "run", run_id)


def _effect_is_uncertain(session: Session, installation_id: str) -> bool:
    """Whether the last operation on an installation left its effect unproven.

    Only an install the agent reported as failed is definitive: the agent stopped
    writing. A cancelled or operator-retired order, or a failed cleanup, may
    have left a write running, and the exact cleanup owns what to do with it.
    """

    last = session.scalar(
        select(Job)
        .where(
            Job.payload["owner_kind"].as_string() == "installation",
            Job.payload["owner_id"].as_string() == installation_id,
        )
        .order_by(Job.updated_at.desc(), Job.id.desc())
        .limit(1)
    )
    if last is None:
        return False
    result = last.result if isinstance(last.result, dict) else {}
    return (
        last.kind != "recipe.install"
        or last.state == "cancelled"
        or bool(result.get("cancelled"))
        or bool(result.get("cancel_requested"))
    )


def _a_rank_shows_an_effect(session: Session, installation_id: str) -> bool:
    """Whether a planned installation's member already reached its Spark."""

    return any(
        member.state != "planned" or member.installed_bytes
        for member in session.scalars(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation_id
            )
        )
    )


def reservation_owner(
    session: Session,
    owner_kind: str,
    owner_id: str,
    *,
    now: datetime | None = None,
    claim_created_at: datetime | None = None,
) -> ReservationOwner:
    """Classify one owner. Anything uncertain is live: a release needs proof.

    Without ``now`` the settling grace and adoption window are not applied, which
    is how an explanation names an owner; the reconciler always passes ``now``.
    """

    if owner_kind == "fleet-profile":
        noun = "profile application"
        application = session.get(FleetProfileApplication, owner_id)
        if application is None:
            return ReservationOwner(owner_kind, owner_id, "missing", True, noun)
        return ReservationOwner(
            owner_kind,
            owner_id,
            application.state,
            application.state in _ENDED_APPLICATION_STATES,
            noun,
        )
    if owner_kind == "run":
        noun = "run"
        run = session.get(RecipeRun, owner_id)
        if run is None:
            return ReservationOwner(owner_kind, owner_id, "missing", True, noun)
        return ReservationOwner(
            owner_kind,
            owner_id,
            run.state,
            run.state not in STOPPABLE_RUN_STATES,
            noun,
        )
    if owner_kind == "installation":
        return _installation_owner(session, owner_id, now)
    if owner_kind == "recipe-build":
        noun = "recipe build"
        build = session.get(RecipeBuild, owner_id)
        if build is None:
            return ReservationOwner(owner_kind, owner_id, "missing", True, noun)
        abandoned = _settled(claim_created_at, now, OWNER_SETTLE_GRACE) and (
            not _referencing_active_job(session, owner_kind, owner_id)
        )
        return ReservationOwner(
            owner_kind,
            owner_id,
            build.state,
            abandoned,
            noun,
            "no active operation" if abandoned else "",
        )
    return ReservationOwner(owner_kind, owner_id, "unrecognised", False, "owner")


def _installation_owner(
    session: Session, owner_id: str, now: datetime | None
) -> ReservationOwner:
    noun = "installation"
    installation = session.get(RecipeInstallation, owner_id)
    if installation is None:
        return ReservationOwner("installation", owner_id, "missing", True, noun)
    state = installation.state
    if state == "uninstalled":
        return ReservationOwner("installation", owner_id, state, True, noun)
    if state == "installed":
        return ReservationOwner("installation", owner_id, state, False, noun)
    window = ADOPTION_WINDOW if state == "planned" else OWNER_SETTLE_GRACE
    if not _settled(installation.updated_at, now, window):
        return ReservationOwner("installation", owner_id, state, False, noun)
    if _referencing_active_job(session, "installation", owner_id):
        return ReservationOwner("installation", owner_id, state, False, noun)
    if (
        _effect_is_uncertain(session, owner_id)
        if state != "planned"
        else _a_rank_shows_an_effect(session, owner_id)
    ):
        return ReservationOwner(
            "installation", owner_id, state, False, noun, "awaiting exact cleanup"
        )
    return ReservationOwner(
        "installation", owner_id, state, True, noun, "no active operation"
    )


def release_dead_owner_reservations(
    session: Session, now: datetime
) -> tuple[ReservationOwner, ...]:
    """Release every active or promised claim whose owner is dead.

    Candidates are found without locks. Only a candidate's rows are locked, and
    a row another transaction holds (an admission handing it off, a profile
    re-reserving it) is skipped until the next tick instead of waited for, so
    this never blocks or fails the writer it races. The owner is classified
    again once its rows are held. Releasing an already released claim is a
    no-op, and an owner that resumes (a profile retry, an install retry)
    takes its claim back.
    """

    released: list[ReservationOwner] = []
    owners = session.execute(
        select(ResourceReservation.owner_kind, ResourceReservation.owner_id)
        .where(ResourceReservation.state.in_(("active", "promised")))
        .distinct()
    ).all()
    for owner_kind, owner_id in sorted(owners):
        if not reservation_owner(session, owner_kind, owner_id, now=now).dead:
            continue
        claims = session.scalars(
            select(ResourceReservation)
            .where(
                ResourceReservation.owner_kind == owner_kind,
                ResourceReservation.owner_id == owner_id,
                ResourceReservation.state.in_(("active", "promised")),
            )
            .order_by(ResourceReservation.id)
            .with_for_update(skip_locked=True)
        ).all()
        if not claims:
            continue
        owner = reservation_owner(
            session,
            owner_kind,
            owner_id,
            now=now,
            claim_created_at=max(claim.created_at for claim in claims),
        )
        if not owner.dead:
            continue
        for claim in claims:
            claim.state = "released"
            claim.released_at = now
        released.append(owner)
    return tuple(released)


def release_covered_installation_claims(
    session: Session, now: datetime
) -> tuple[ReservationOwner, ...]:
    """Release the disk claim of an installation whose files the Spark now reports.

    A claim promises bytes the install is still to write. Once the installation
    is installed on a Spark and that Spark has reported its disk after the
    install completed (past the accepted agent-clock skew), the written bytes
    are already subtracted from the reported free space, so the claim counts
    them a second time, and the install's staging space is gone with it. The
    claim is then released, whatever the plan says, until a run of the
    installation is live: a serving workload still writes its caches, and
    those bytes are not in any observation yet. Releasing earlier would let
    admission spend free space an unobserved install is about to use.
    """

    released: list[ReservationOwner] = []
    rows = session.execute(
        select(ResourceReservation, RecipeInstallation, InstallationNode)
        .join(
            RecipeInstallation,
            ResourceReservation.owner_id == RecipeInstallation.id,
        )
        .join(
            InstallationNode,
            (InstallationNode.installation_id == RecipeInstallation.id)
            & (InstallationNode.node_id == ResourceReservation.node_id),
        )
        .where(
            ResourceReservation.owner_kind == "installation",
            ResourceReservation.kind == "disk",
            ResourceReservation.state == "active",
            RecipeInstallation.state == "installed",
            InstallationNode.state == "installed",
        )
    ).all()
    for reservation, installation, node in rows:
        observed = session.scalar(
            select(func.max(NodeInventorySnapshot.observed_at)).where(
                NodeInventorySnapshot.node_id == reservation.node_id
            )
        )
        if observed is None or _aware(observed) <= (
            _aware(node.updated_at) + MAX_INVENTORY_FUTURE_SKEW
        ):
            continue
        if (
            session.scalar(
                select(RecipeRun.id)
                .where(
                    RecipeRun.installation_id == installation.id,
                    RecipeRun.state.in_(STOPPABLE_RUN_STATES),
                )
                .limit(1)
            )
            is not None
        ):
            continue
        locked = session.scalar(
            select(ResourceReservation)
            .where(
                ResourceReservation.id == reservation.id,
                ResourceReservation.state == "active",
            )
            .with_for_update(skip_locked=True)
        )
        if locked is None:
            continue
        locked.state = "released"
        locked.released_at = now
        released.append(
            ReservationOwner(
                "installation",
                installation.id,
                "installed",
                False,
                "installation",
                "files already counted in free space",
            )
        )
    return tuple(released)


def _settled(since: datetime | None, now: datetime | None, window: timedelta) -> bool:
    return since is None or now is None or _aware(since) + window <= _aware(now)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
