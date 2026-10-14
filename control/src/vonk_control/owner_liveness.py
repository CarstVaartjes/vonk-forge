"""Whether the owner of a claim or a wait can still finish its work.

A claim (a reservation, a build row, a removal fence, a sync slot, a renewal
intent, a node's mutation turn) is worth holding only while its owner can still
do what it holds the claim for. A wait is worth keeping only while the owner it
waits on can still finish. ``owner()`` answers both, for every owner kind a
claim table or a wait names, from the owner's own evidence: its lifecycle
state, a live Job or attempt that refers to it, or the heartbeat it writes.
Never the claim's age.

The rule is symmetric: a claim is reclaimed iff its owner ended, and is never
taken from a live one. A sweep that runs on age may only *ask* ``owner()``.
Anything uncertain is live: releasing needs proof. Without ``now`` no window
is applied, which is how an explanation names an owner.

Kept on purpose by the installation arm:

* an installation that is ``installed`` is live; once its Spark reported its
  disk after the install the files are in the reported free space and its claim
  is released as covered (``covered_installation_claims``), unless a run of it
  is live;
* an installation whose last operation was cancelled or retired, or was a
  cleanup (uninstall, reconcile): its effect on the Spark is uncertain, so it
  stays live until the exact cleanup proves the effect gone.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import Text, cast, exists, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    LifecycleState,
    ProgressPhase,
    canonical_message,
)
from vonk_agent_protocol.state_machines import EnrollmentRecordState

from . import agent_operation_states, job_states, model_cache_states
from .fleet_profile_contract import (
    FLEET_PROFILE_ENDED_STATES,
    FleetProfileApplicationProgress,
)
from .models import (
    STOPPABLE_RUN_STATES,
    AgentCertificateRotation,
    AgentEnrollment,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    FleetProfileApplication,
    InstallationNode,
    Job,
    ModelCacheOperation,
    RecipeBuild,
    RecipeInstallation,
    RecipeLibrarySyncRun,
    RecipeRun,
    ResourceReservation,
)
from .strict_json import read_stored_model

#: A Job in one of these has ended; nothing it owns can still be used by it.
JOB_ENDED_STATES = job_states.words(
    LifecycleState.SUCCEEDED,
    LifecycleState.FAILED,
    LifecycleState.CANCELLED,
    LifecycleState.SUPERSEDED,
)
#: A model-cache operation in one of these has ended.
CACHE_OPERATION_ENDED_STATES = model_cache_states.words(
    LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED
)
_ACTIVE_JOB_STATES = (LifecycleState.QUEUED.value, LifecycleState.RUNNING.value)
#: A build row in one of these has not produced its image yet.
UNFINISHED_BUILD_STATES = (
    InstallationState.PLANNED.value,
    ProgressPhase.BUILDING.value,
)
#: A Spark not heard from for longer is away (the Fleet view's offline bound).
SPARK_PRESENCE_WINDOW = timedelta(seconds=150)
# How long an unowned plan waits for the next attempt to adopt it.
ADOPTION_WINDOW = timedelta(minutes=15)
# A claim is created in the same transaction as the operation that owns it; this
# only absorbs a reader that sees an owner's state change before its successor.
OWNER_SETTLE_GRACE = timedelta(minutes=5)
#: A running library sync heartbeats while it works; silent this long, it died.
SYNC_HEARTBEAT_WINDOW = timedelta(minutes=10)
#: A certificate issuance is touched by every request that renews it; untouched
#: this long, the process that issued it is gone.
ISSUANCE_WINDOW = timedelta(minutes=5)


class OwnerKind(StrEnum):
    """The owners a claim or a wait can be held for.

    A value equal to a stored ``owner_kind`` (a reservation's, a removal
    fence's) names the same owner, so a stored kind dispatches as it is.
    """

    FLEET_PROFILE = "fleet-profile"
    NODE = "node"
    SPARK_RETURN = "spark-return"
    JOB = "job"
    RUN = "run"
    INSTALLATION = "installation"
    RECIPE_BUILD = "recipe-build"
    AGENT_ATTEMPT = "agent-attempt"
    CACHE_OPERATION = "model-cache-operation"
    IMAGE_JOB = "recipe-image-job"
    SYNC_RUN = "sync-run"
    RENEWAL_INTENT = "renewal-intent"
    ENROLLMENT = "enrollment"


@dataclass(frozen=True, slots=True)
class OwnerRef:
    """Whom a claim or a wait is held for, and its id."""

    kind: OwnerKind
    id: str


@dataclass(frozen=True, slots=True)
class Owner:
    """Whether an owner can still finish what its claim or wait is for."""

    ref: OwnerRef
    live: bool
    why: str
    #: When an ended owner ended, if known: a wait's window restarts there.
    ended_at: datetime | None = None
    #: The owner's stored state word (``missing`` for a gone owner).
    state: str = ""


def owner(session: Session, ref: OwnerRef, now: datetime | None = None) -> Owner:
    """Resolve an owner. Anything uncertain is live: ending needs proof.

    Live while:

    * ``fleet-profile``: the application has not ended;
    * ``node``: the Spark is enrolled and not revoked (an offline or retired
      Spark may return);
    * ``spark-return``: the enrolled Spark has not been heard from for
      ``SPARK_PRESENCE_WINDOW`` (``now`` required); a present Spark is not
      waited on;
    * ``job`` / ``recipe-image-job``: the Job has not ended;
    * ``run``: the run is in a state a Stop can still act on;
    * ``installation``: see the module docstring;
    * ``recipe-build``: a not-yet-ended Job names the build (its own build Job
      or a parent that recorded it), or the row settled less than
      ``OWNER_SETTLE_GRACE`` ago;
    * ``agent-attempt`` (an agent operation id): its current attempt holds the
      fence (``attempt_is_live``);
    * ``model-cache-operation``: the operation has not ended;
    * ``sync-run``: the sync is running and heartbeat within
      ``SYNC_HEARTBEAT_WINDOW``;
    * ``renewal-intent`` (a node id): a renewal touched the intent within
      ``ISSUANCE_WINDOW``;
    * ``enrollment``: the enrollment is issuing and was accepted within
      ``ISSUANCE_WINDOW`` (it records no later touch).
    """

    # Reading an owner never flushes the caller's half-built rows.
    with session.no_autoflush:
        arm = _ARMS.get(ref.kind)
        if arm is None:
            return Owner(ref, True, f"unrecognised owner {ref.kind}")
        return arm(session, ref, now)


def _fleet_profile(session: Session, ref: OwnerRef, now: datetime | None) -> Owner:
    application = session.get(FleetProfileApplication, ref.id)
    if application is None:
        return Owner(ref, False, "profile application is missing", state="missing")
    ended = application.state in FLEET_PROFILE_ENDED_STATES
    return Owner(
        ref,
        not ended,
        f"profile application is {application.state}",
        _aware(application.updated_at) if ended else None,
        application.state,
    )


def _node(session: Session, ref: OwnerRef, now: datetime | None) -> Owner:
    node = session.get(AgentNode, ref.id)
    if node is None:
        return Owner(ref, False, "Spark is not enrolled", state="missing")
    revoked = node.revoked_at
    return Owner(
        ref,
        revoked is None,
        f"Spark agent is {node.state}" if revoked is None else "Spark is revoked",
        None if revoked is None else _aware(revoked),
        node.state,
    )


def _spark_return(session: Session, ref: OwnerRef, now: datetime | None) -> Owner:
    node = session.get(AgentNode, ref.id)
    if node is None:
        return Owner(ref, False, "Spark is not enrolled", state="missing")
    if node.revoked_at is not None:
        return Owner(
            ref, False, "Spark is revoked", _aware(node.revoked_at), node.state
        )
    seen = node.last_seen_at
    away = (
        now is None
        or seen is None
        or _aware(now) - _aware(seen) > SPARK_PRESENCE_WINDOW
    )
    return Owner(
        ref, away, "Spark is away" if away else "Spark is present", state=node.state
    )


def _job(session: Session, ref: OwnerRef, now: datetime | None) -> Owner:
    job = session.get(Job, ref.id)
    if job is None:
        return Owner(ref, False, "operation is missing", state="missing")
    ended = job.state in JOB_ENDED_STATES
    return Owner(
        ref,
        not ended,
        f"{job.kind} is {job.state}",
        _aware(job.updated_at) if ended else None,
        job.state,
    )


def _run(session: Session, ref: OwnerRef, now: datetime | None) -> Owner:
    run = session.get(RecipeRun, ref.id)
    if run is None:
        return Owner(ref, False, "run is missing", state="missing")
    live = run.state in STOPPABLE_RUN_STATES
    return Owner(ref, live, f"run is {run.state}", state=run.state)


def _installation(session: Session, ref: OwnerRef, now: datetime | None) -> Owner:
    installation = session.get(RecipeInstallation, ref.id)
    if installation is None:
        return Owner(ref, False, "installation is missing", state="missing")
    state = installation.state
    if state == InstallationState.UNINSTALLED:
        return Owner(ref, False, "uninstalled", state=state)
    if state == InstallationState.INSTALLED:
        return Owner(ref, True, "installed", state=state)
    window = (
        ADOPTION_WINDOW if state == InstallationState.PLANNED else OWNER_SETTLE_GRACE
    )
    if not _settled(installation.updated_at, now, window):
        return Owner(ref, True, "settling", state=state)
    if referencing_active_job(session, OwnerKind.INSTALLATION, ref.id):
        return Owner(ref, True, "an active operation refers to it", state=state)
    if (
        _effect_is_uncertain(session, ref.id)
        if state != InstallationState.PLANNED
        else _a_rank_shows_an_effect(session, ref.id)
    ):
        return Owner(ref, True, "awaiting exact cleanup", state=state)
    return Owner(ref, False, "no active operation", state=state)


def _recipe_build(session: Session, ref: OwnerRef, now: datetime | None) -> Owner:
    build = session.get(RecipeBuild, ref.id)
    if build is None:
        return Owner(ref, False, "recipe build is missing", state="missing")
    if not _settled(build.updated_at, now, OWNER_SETTLE_GRACE):
        return Owner(ref, True, "settling", state=build.state)
    if build.state == ProgressPhase.BUILDING.value:
        if referencing_active_job(session, OwnerKind.RECIPE_BUILD, ref.id):
            return Owner(
                ref, True, "an active operation refers to it", state=build.state
            )
        why = "no active operation"
    else:
        # A parent kept open for a cleanup that will not come, or another job
        # naming the build, does not keep a build that stopped.
        if build_may_run(session, build):
            return Owner(
                ref, True, "one of its own orders may still run", state=build.state
            )
        why = "its build and cleanup orders have ended"
    return Owner(ref, False, why, _aware(build.updated_at), build.state)


def build_may_run(session: Session, build: RecipeBuild) -> bool:
    """Whether a build may still write or allocate on its Spark.

    It may while it is building, or while one of its own Spark orders (the
    build, a cleanup) has not ended or a job of it has none yet. A build that
    stopped and whose orders ended does nothing more: what it left is in the
    Spark's observed inventory.
    """

    if build.state == ProgressPhase.BUILDING.value:
        return True
    for job_id in session.scalars(
        select(Job.id).where(
            Job.state.not_in(JOB_ENDED_STATES),
            Job.payload["owner_kind"].as_string() == OwnerKind.RECIPE_BUILD.value,
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


def _agent_attempt(session: Session, ref: OwnerRef, now: datetime | None) -> Owner:
    from .agent_operation_facts import attempt_is_live

    operation = session.get(AgentOperation, ref.id)
    if operation is None:
        return Owner(ref, False, "agent order is missing", state="missing")
    attempt = session.scalar(
        select(AgentOperationAttempt).where(
            AgentOperationAttempt.operation_id == operation.id,
            AgentOperationAttempt.attempt == operation.current_attempt,
        )
    )
    if now is None:
        return Owner(ref, True, "attempt is not judged", state=operation.state)
    live = attempt_is_live(operation, attempt, now)
    return Owner(
        ref,
        live,
        "attempt holds the fence" if live else "no attempt holds the fence",
        state=operation.state,
    )


def _cache_operation(session: Session, ref: OwnerRef, now: datetime | None) -> Owner:
    operation = session.get(ModelCacheOperation, ref.id)
    if operation is None:
        return Owner(ref, False, "model cache operation is missing", state="missing")
    ended = operation.state in CACHE_OPERATION_ENDED_STATES
    return Owner(
        ref,
        not ended,
        f"model cache operation is {operation.state}",
        _aware(operation.updated_at) if ended else None,
        operation.state,
    )


def _sync_run(session: Session, ref: OwnerRef, now: datetime | None) -> Owner:
    run = session.get(RecipeLibrarySyncRun, ref.id)
    if run is None:
        return Owner(ref, False, "library sync is missing", state="missing")
    if run.state != LifecycleState.RUNNING:
        return Owner(ref, False, f"library sync is {run.state}", state=run.state)
    last = run.heartbeat_at or run.started_at
    if now is not None and _aware(now) - _aware(last) > SYNC_HEARTBEAT_WINDOW:
        return Owner(
            ref, False, "library sync stopped heartbeating", _aware(last), run.state
        )
    return Owner(ref, True, "library sync is heartbeating", state=run.state)


def _renewal_intent(session: Session, ref: OwnerRef, now: datetime | None) -> Owner:
    intent = session.get(AgentCertificateRotation, ref.id)
    if intent is None:
        return Owner(ref, False, "certificate renewal is missing", state="missing")
    touched = _aware(intent.updated_at)
    if now is not None and _aware(now) - touched >= ISSUANCE_WINDOW:
        return Owner(ref, False, "no renewal touched the intent", touched, intent.state)
    return Owner(ref, True, "a renewal is touching the intent", state=intent.state)


def _enrollment(session: Session, ref: OwnerRef, now: datetime | None) -> Owner:
    enrollment = session.get(AgentEnrollment, ref.id)
    if enrollment is None:
        return Owner(ref, False, "enrollment is missing", state="missing")
    if enrollment.state != EnrollmentRecordState.ISSUING:
        return Owner(
            ref, False, f"enrollment is {enrollment.state}", state=enrollment.state
        )
    accepted = _aware(enrollment.created_at)
    if now is not None and _aware(now) - accepted >= ISSUANCE_WINDOW:
        return Owner(
            ref, False, "enrollment issuance was abandoned", state=enrollment.state
        )
    return Owner(ref, True, "enrollment is issuing", state=enrollment.state)


_ARMS: dict[OwnerKind, Callable[[Session, OwnerRef, datetime | None], Owner]] = {
    OwnerKind.FLEET_PROFILE: _fleet_profile,
    OwnerKind.NODE: _node,
    OwnerKind.SPARK_RETURN: _spark_return,
    OwnerKind.JOB: _job,
    OwnerKind.IMAGE_JOB: _job,
    OwnerKind.RUN: _run,
    OwnerKind.INSTALLATION: _installation,
    OwnerKind.RECIPE_BUILD: _recipe_build,
    OwnerKind.AGENT_ATTEMPT: _agent_attempt,
    OwnerKind.CACHE_OPERATION: _cache_operation,
    OwnerKind.SYNC_RUN: _sync_run,
    OwnerKind.RENEWAL_INTENT: _renewal_intent,
    OwnerKind.ENROLLMENT: _enrollment,
}


def referencing_active_job(session: Session, owner_kind: str, owner_id: str) -> bool:
    """Whether a not-yet-ended operation was issued for, or records, this owner.

    An operation issued for the owner names it in its payload; a parent order
    (a Run/Switch that prepared an installation or a build, or a profile)
    records the id in its progress. Either keeps the owner live.
    """

    return (
        session.scalar(
            select(Job.id)
            .where(
                Job.state.not_in(JOB_ENDED_STATES),
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

    return referencing_active_job(session, OwnerKind.RUN, run_id)


def live_build_job(session: Session, build: RecipeBuild) -> Job | None:
    """The queued or running build Job that owns this build's exact input."""

    return session.scalar(
        select(Job)
        .where(
            Job.kind == WireAgentOperation.RECIPE_BUILD.value,
            Job.state.in_(_ACTIVE_JOB_STATES),
            Job.payload["owner_id"].as_string() == build.id,
            Job.payload["plan_digest"].as_string() == build.build_input_sha256,
        )
        .order_by(Job.updated_at.desc(), Job.id)
        .limit(1)
    )


def build_in_flight(session: Session, build: RecipeBuild, now: datetime) -> bool:
    """Whether a build that has not produced its image still has a live owner."""

    return (
        build.state in UNFINISHED_BUILD_STATES
        and owner(session, OwnerRef(OwnerKind.RECIPE_BUILD, build.id), now).live
    )


def builds_in_flight(now: datetime) -> ColumnElement[bool]:
    """``build_in_flight`` as a filter on ``RecipeBuild`` rows: the same rule.

    An unfinished row that settled within ``OWNER_SETTLE_GRACE``; a building
    row that a not-yet-ended Job names; a planned row one of whose own orders
    may still run.
    """

    own_job = (
        Job.payload["owner_kind"].as_string() == OwnerKind.RECIPE_BUILD.value
    ) & (Job.payload["owner_id"].as_string() == RecipeBuild.id)
    may_run = exists().where(
        Job.state.not_in(JOB_ENDED_STATES),
        own_job,
        or_(
            ~exists().where(AgentOperation.parent_job_id == Job.id),
            exists().where(
                AgentOperation.parent_job_id == Job.id,
                AgentOperation.state.in_(agent_operation_states.LIVE),
            ),
        ),
    )
    referenced = exists().where(
        Job.state.not_in(JOB_ENDED_STATES),
        own_job | cast(Job.result, Text).contains(RecipeBuild.id),
    )
    return RecipeBuild.state.in_(UNFINISHED_BUILD_STATES) & or_(
        RecipeBuild.updated_at > now - OWNER_SETTLE_GRACE,
        (RecipeBuild.state == ProgressPhase.BUILDING.value) & referenced,
        (RecipeBuild.state != ProgressPhase.BUILDING.value) & may_run,
    )


def profile_claim_consumable(session: Session, claim: ResourceReservation) -> bool:
    """Whether a profile load's promise can still be handed to its run.

    Its owner, the profile application, must be live, and the workload intent
    it was reviewed against must still be its Spark's: a newer intent fenced
    it, and its run will never consume the promise. A progress document that
    cannot be read names no intent, so nothing can consume the promise.
    """

    if not owner(session, OwnerRef(OwnerKind.FLEET_PROFILE, claim.owner_id)).live:
        return False
    application = session.get(FleetProfileApplication, claim.owner_id)
    node = session.get(AgentNode, claim.node_id)
    if application is None or node is None:
        return False
    try:
        progress = read_stored_model(
            FleetProfileApplicationProgress,
            canonical_message(application.progress),
            strict=True,
            from_json=True,
        )
    except (TypeError, ValueError):
        return False
    return progress.workload_intent_ordinal == node.workload_intent_ordinal


def _effect_is_uncertain(session: Session, installation_id: str) -> bool:
    """Whether the last operation on an installation left its effect unproven.

    Only an install the agent reported as failed is definitive: the agent stopped
    writing. A cancelled or operator-retired order, or a failed cleanup, may
    have left a write running, and the exact cleanup owns what to do with it.
    """

    last = session.scalar(
        select(Job)
        .where(
            Job.payload["owner_kind"].as_string() == OwnerKind.INSTALLATION.value,
            Job.payload["owner_id"].as_string() == installation_id,
        )
        .order_by(Job.updated_at.desc(), Job.id.desc())
        .limit(1)
    )
    if last is None:
        return False
    result = last.result if isinstance(last.result, dict) else {}
    return (
        last.kind != WireAgentOperation.RECIPE_INSTALL.value
        or last.state == LifecycleState.CANCELLED.value
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


def _settled(since: datetime | None, now: datetime | None, window: timedelta) -> bool:
    return since is None or now is None or _aware(since) + window <= _aware(now)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
