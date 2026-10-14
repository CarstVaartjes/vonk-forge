"""Who owns a resource reservation, and whether that owner can still use it.

A reservation (disk, port, memory) is a promise that some operation is about to
write, bind or allocate. It is worth holding only while its owner can still do
that. The owner is *dead* when it no longer exists or has ended in a state that
cannot continue without asking again: a cancelled or failed profile application,
a stopped run, an uninstalled installation, an installation or building build
that no live operation refers to any more, a build that stopped and whose own
Spark orders have ended. A dead owner's physical effects are what the
Spark's next inventory reports; its reservation is bookkeeping that would
otherwise block admission forever.

This module is the one predicate. The reconciler releases what it names dead,
and the admission blockers name the owners of what they count, from the same
decision, so an explanation and a release never disagree.

Kept on purpose:

* an installation that is ``installed``: its claim is not "dead", but once its
  Spark has reported its disk after the install completed the files are in the
  reported free space and the claim is released as covered
  (``covered_installation_claims``), unless a run of it is live;
* an installation whose last operation was cancelled or retired, or was a
  cleanup (uninstall, reconcile): its effect on the Spark is uncertain, so its
  claim stays until the exact cleanup proves the effect gone;
* anything a live operation still refers to, and any owner kind not listed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import Text, cast, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    LifecycleState,
    ReservationState,
)

from . import agent_operation_states, job_states
from .fleet_profile_contract import FLEET_PROFILE_ENDED_STATES
from .models import (
    STOPPABLE_RUN_STATES,
    AgentNode,
    AgentOperation,
    FleetProfileApplication,
    InstallationNode,
    Job,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)

_TERMINAL_JOB_STATES = job_states.words(
    LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED
)
_ENDED_APPLICATION_STATES = FLEET_PROFILE_ENDED_STATES
#: A Spark not heard from for longer is away (the Fleet view's offline bound).
SPARK_PRESENCE_WINDOW = timedelta(seconds=150)
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
        member.state != InstallationNodeState.PLANNED or member.installed_bytes
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
        return _build_owner(session, owner_id, now, claim_created_at)
    return ReservationOwner(owner_kind, owner_id, "unrecognised", False, "owner")


class OwnerKind(StrEnum):
    """The owners a wait can wait on."""

    FLEET_PROFILE = "fleet-profile"
    NODE = "node"
    SPARK_RETURN = "spark-return"
    JOB = "job"


@dataclass(frozen=True, slots=True)
class OwnerRef:
    """Whom a wait waits on, and its id."""

    kind: OwnerKind
    id: str


@dataclass(frozen=True, slots=True)
class Owner:
    """Whether a waited-on owner can still finish what the wait is for."""

    ref: OwnerRef
    live: bool
    why: str
    #: When an ended owner ended, if known: a wait's window restarts there.
    ended_at: datetime | None = None


def owner(session: Session, ref: OwnerRef, now: datetime | None = None) -> Owner:
    """Resolve the owner a wait is on. Anything uncertain is live: ending needs proof.

    * ``fleet-profile``: an application that has not ended;
    * ``node``: an enrolled Spark that is not revoked; an offline or retired
      Spark may return (re-enrolment), so a wait for its presence is a wait on
      a live owner;
    * ``spark-return``: work held by its Spark being away (``now`` required):
      live while the enrolled Spark has not been heard from for
      ``SPARK_PRESENCE_WINDOW``. A present Spark is not waited on: work it
      does not advance there has stalled;
    * ``job``: a lifecycle operation (an image preparation, a start, a removal)
      that has not ended: the lifecycle core ends it when it stalls.
    """

    # Reading an owner never flushes the caller's half-built rows.
    with session.no_autoflush:
        return _owner(session, ref, now)


def _owner(session: Session, ref: OwnerRef, now: datetime | None) -> Owner:
    if ref.kind is OwnerKind.FLEET_PROFILE:
        application = session.get(FleetProfileApplication, ref.id)
        if application is None:
            return Owner(ref, False, "profile application is missing")
        ended = application.state in _ENDED_APPLICATION_STATES
        return Owner(
            ref,
            not ended,
            f"profile application is {application.state}",
            _aware(application.updated_at) if ended else None,
        )
    if ref.kind is OwnerKind.NODE:
        node = session.get(AgentNode, ref.id)
        if node is None:
            return Owner(ref, False, "Spark is not enrolled")
        revoked = node.revoked_at
        return Owner(
            ref,
            revoked is None,
            f"Spark agent is {node.state}" if revoked is None else "Spark is revoked",
            None if revoked is None else _aware(revoked),
        )
    if ref.kind is OwnerKind.SPARK_RETURN:
        return _spark_return(session, ref, now)
    if ref.kind is OwnerKind.JOB:
        job = session.get(Job, ref.id)
        if job is None:
            return Owner(ref, False, "operation is missing")
        ended = job.state in _TERMINAL_JOB_STATES
        return Owner(
            ref,
            not ended,
            f"{job.kind} is {job.state}",
            _aware(job.updated_at) if ended else None,
        )
    return Owner(ref, True, f"unrecognised owner {ref.kind}")


def _spark_return(session: Session, ref: OwnerRef, now: datetime | None) -> Owner:
    node = session.get(AgentNode, ref.id)
    if node is None:
        return Owner(ref, False, "Spark is not enrolled")
    if node.revoked_at is not None:
        return Owner(ref, False, "Spark is revoked", _aware(node.revoked_at))
    seen = node.last_seen_at
    away = (
        now is None
        or seen is None
        or _aware(now) - _aware(seen) > SPARK_PRESENCE_WINDOW
    )
    return Owner(ref, away, "Spark is away" if away else "Spark is present")


def _build_owner(
    session: Session,
    owner_id: str,
    now: datetime | None,
    claim_created_at: datetime | None,
) -> ReservationOwner:
    noun = "recipe build"
    build = session.get(RecipeBuild, owner_id)
    if build is None:
        return ReservationOwner("recipe-build", owner_id, "missing", True, noun)
    if build.state == "building":
        abandoned = _settled(claim_created_at, now, OWNER_SETTLE_GRACE) and (
            not _referencing_active_job(session, "recipe-build", owner_id)
        )
        detail = "no active operation"
    else:
        # A parent kept open for a cleanup that will not come, or another job
        # naming the build, does not keep the claims of a build that stopped.
        abandoned = _settled(
            claim_created_at, now, OWNER_SETTLE_GRACE
        ) and not build_may_run(session, build)
        detail = "its build and cleanup orders have ended"
    return ReservationOwner(
        "recipe-build",
        owner_id,
        build.state,
        abandoned,
        noun,
        detail if abandoned else "",
    )


def build_may_run(session: Session, build: RecipeBuild) -> bool:
    """Whether a build may still write or allocate on its Spark.

    It may while it is building, or while one of its own Spark orders (the
    build, a cleanup) has not ended or a job of it has none yet. A build that
    stopped and whose orders ended does nothing more: what it left is in the
    Spark's observed inventory.
    """

    if build.state == "building":
        return True
    for job_id in session.scalars(
        select(Job.id).where(
            Job.state.not_in(_TERMINAL_JOB_STATES),
            Job.payload["owner_kind"].as_string() == "recipe-build",
            Job.payload["owner_id"].as_string() == build.id,
        )
    ):
        states = set(
            session.scalars(
                select(AgentOperation.state).where(
                    AgentOperation.parent_job_id == job_id
                )
            )
        )
        if not states or states & set(agent_operation_states.LIVE):
            return True
    return False


def _installation_owner(
    session: Session, owner_id: str, now: datetime | None
) -> ReservationOwner:
    noun = "installation"
    installation = session.get(RecipeInstallation, owner_id)
    if installation is None:
        return ReservationOwner("installation", owner_id, "missing", True, noun)
    state = installation.state
    if state == InstallationState.UNINSTALLED:
        return ReservationOwner("installation", owner_id, state, True, noun)
    if state == InstallationState.INSTALLED:
        return ReservationOwner("installation", owner_id, state, False, noun)
    window = (
        ADOPTION_WINDOW if state == InstallationState.PLANNED else OWNER_SETTLE_GRACE
    )
    if not _settled(installation.updated_at, now, window):
        return ReservationOwner("installation", owner_id, state, False, noun)
    if _referencing_active_job(session, "installation", owner_id):
        return ReservationOwner("installation", owner_id, state, False, noun)
    if (
        _effect_is_uncertain(session, owner_id)
        if state != InstallationState.PLANNED
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
        .where(
            ResourceReservation.state.in_(
                (ReservationState.ACTIVE, ReservationState.PROMISED)
            )
        )
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
                ResourceReservation.state.in_(
                    (ReservationState.ACTIVE, ReservationState.PROMISED)
                ),
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
            claim.state = ReservationState.RELEASED
            claim.released_at = now
        released.append(owner)
    return tuple(released)


def _settled(since: datetime | None, now: datetime | None, window: timedelta) -> bool:
    return since is None or now is None or _aware(since) + window <= _aware(now)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
