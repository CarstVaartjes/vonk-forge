"""Durable controller coordination for distributed recipe recovery."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    InstallationState,
    InvalidRequestReason,
    RecipeStartPayload,
    ReservationState,
    RouteState,
    RunState,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)
from vonk_agent_protocol.route_activation import ROUTE_EVIDENCE_MAX_AGE_SECONDS
from vonk_forge_contracts import RecipeDefinition, read_recipe

from .agent_jobs import release_owned_reservations_in_session
from .categorized_errors import UnsettledOutcome
from .distributed_lifecycle import (
    DistributedLifecycleError,
    DistributedRecoveryInvalid,
    canonical_distributed_readiness,
)
from .lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from .lifecycle.job import JobAdapter
from .litellm import LiteLlmGeneration
from .models import (
    STOPPABLE_NOT_RUNNING_RUN_STATES,
    STOPPABLE_RUN_STATES,
    AgentNode,
    AgentOperation,
    AgentPresence,
    CatalogDocumentRevision,
    ClusterMapping,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    installation_plan_document,
    run_plan_document,
)
from .recipe_start_payloads import (
    RecipeStartPayloadError,
    RecipeStartPlacement,
    build_recipe_start_payload,
    validate_distributed_start_timeout_seconds,
)
from .recipe_stop_payloads import (
    RecipeStopAuthorityError,
    durable_run_stop_payloads,
)
from .reservation_owners import run_has_live_operation
from .strict_json import read_stored_model

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RECOVERY_RECHECK_SECONDS = 5
_RECOVERY_MAX_ATTEMPTS = 5
_RECOVERY_COOLDOWN_SECONDS = 300

#: Recovery replays a recorded Start only under exact, current authority. A record
#: that is damaged, missing or no longer matches what is observed is *unknown*:
#: it is retired as typed residue and the run is settled with its reason (its
#: claims are released and a new authorised request starts from the live state).
#: It never raises out of the derivation and never parks the run for an operator.
_DAMAGED = BookkeepingReason.PERSISTED_STATE_DAMAGED
_MISMATCH = BookkeepingReason.EVIDENCE_MISMATCH
_MISSING = BookkeepingReason.ROW_INCOMPLETE


def _unproven(
    subject: str,
    reason: BookkeepingReason,
    message: str,
    cause: BaseException | None = None,
) -> Residue:
    """Retire recovery authority that cannot be proven; the caller settles on it.

    The operator-visible ``message`` stays the stable cause text; the cause's type
    (never its text, which can carry stored payload values) joins the residue note.
    """

    note = message if cause is None else f"{message} ({type(cause).__name__})"
    return retire_as_unknown("distributed-recovery.authority", subject, reason, note)


def _active_recipe_revision(
    session: Session, revision_id: str
) -> tuple[CatalogDocumentRevision, RecipeDefinition] | None:
    revision = session.get(CatalogDocumentRevision, revision_id)
    if (
        revision is None
        or revision.kind != "recipe"
        or revision.schema_version != 2
        or revision.state != "active"
    ):
        return None
    try:
        recipe = read_recipe(revision.document)
    except (TypeError, ValueError):
        return None
    return revision, recipe


class _RecoveryJobQueue(Protocol):
    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        authority_revision: str,
        payload: Mapping[str, object],
        *,
        operation_id: str,
    ) -> AgentOperation: ...

    def notify_available(self) -> None: ...


class _RecoveryRoutes(Protocol):
    def publication_transaction(self) -> AbstractContextManager[Session]: ...

    def withdraw_runs(
        self,
        run_ids: Iterable[str],
        *,
        pending: Literal["stop", "recovery"] | None = None,
    ) -> LiteLlmGeneration | None: ...

    def withdrawal_complete_in_session(
        self, session: Session, run_ids: Iterable[str]
    ) -> bool: ...


class _RecoveryRunStops(Protocol):
    def queue_recovery_stop_in_session(
        self,
        session: Session,
        run_id: str,
        *,
        recovery_context: Mapping[str, object],
        workload_intent_ordinal: int,
        now: datetime,
    ) -> Job | Residue: ...


class _RecoveryDependencyPending(UnsettledOutcome):
    """A recoverable precondition is not ready yet; keep the run current."""


class DistributedRecoveryCoordinator:
    """Queue one durable, bounded stop/start recovery for a failed exact rank set."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        routes: _RecoveryRoutes,
        agent_jobs: _RecoveryJobQueue,
        clock: Callable[[], datetime],
        recovery_run_stops: _RecoveryRunStops | None = None,
        singleton_start_timeout_seconds: int = 3600,
    ) -> None:
        self._sessions = sessions
        self._routes = routes
        self._agent_jobs = agent_jobs
        self._clock = clock
        self._recovery_run_stops = recovery_run_stops
        self._singleton_start_timeout_seconds = (
            validate_distributed_start_timeout_seconds(singleton_start_timeout_seconds)
        )

    def tick(self) -> bool:
        now = _aware(self._clock())
        queued = False
        # Claim, effect, conditional completion: the routes of runs about to be
        # recovered or settled are withdrawn with no transaction open, and the
        # transaction below acts on a run only while that withdrawal is
        # complete. A crash in between resumes here, from the durable intent.
        worked = self._withdraw_routes(now)
        with self._routes.publication_transaction() as session:
            worked = self._settle_unreadable_runs(session, now) or worked
            candidates = tuple(
                session.scalars(
                    select(RecipeRun)
                    .where(
                        RecipeRun.state == RunState.RUNNING,
                        or_(
                            RecipeRun.route_next_attempt_at.is_(None),
                            RecipeRun.route_next_attempt_at <= now,
                        ),
                    )
                    .order_by(RecipeRun.created_at, RecipeRun.id)
                    .with_for_update(of=RecipeRun)
                )
            )
            for run in candidates:
                failed = tuple(
                    session.scalars(
                        select(RunNode)
                        .where(
                            RunNode.run_id == run.id, RunNode.state == RunState.FAILED
                        )
                        .order_by(RunNode.rank)
                    )
                )
                if not failed:
                    continue
                run_nodes = tuple(
                    session.scalars(
                        select(RunNode)
                        .where(RunNode.run_id == run.id)
                        .order_by(RunNode.rank)
                    )
                )
                singleton = len(run_nodes) == 1
                if not self._routes.withdrawal_complete_in_session(
                    session, frozenset({run.id})
                ):
                    continue  # withdrawn first on the next tick
                if self._active_recovery(session, run.id):
                    continue
                if _superseded(session, run, run_nodes):
                    _settle_unrecoverable(
                        run,
                        "a newer workload intent owns this run's Sparks; it is "
                        "settled instead of recovered",
                        now,
                    )
                    worked = True
                    continue
                run_plan = run_plan_document(run.plan)
                if run_plan.get("execution_mode") == "one-shot-jobs":
                    _settle_unrecoverable(
                        run,
                        "automatic recovery stopped because a one-shot job may "
                        "have completed external effects before its result was "
                        "lost; verify those effects, then submit a new authorized run",
                        now,
                    )
                    worked = True
                    continue
                if run.recovery_attempts > _RECOVERY_MAX_ATTEMPTS:
                    run.recovery_attempts = 0
                    run.route_error = (
                        "recovery cooldown elapsed; resuming exact inspection"
                    )
                    run.route_next_attempt_at = None
                    run.updated_at = now
                if run.recovery_attempts >= _RECOVERY_MAX_ATTEMPTS:
                    # Keep the degraded reason visible during a finite cooldown.
                    # Once due, reset this retry window and resume automatically.
                    run.route_error = (
                        "recovery is degraded after "
                        f"{_RECOVERY_MAX_ATTEMPTS} attempts; automatic "
                        "recovery will resume after a five minute cooldown"
                    )
                    run.route_next_attempt_at = now + timedelta(
                        seconds=_RECOVERY_COOLDOWN_SECONDS
                    )
                    run.updated_at = now
                    # This marker distinguishes the cooldown row when it becomes
                    # due; no new operator request or authority is required.
                    run.recovery_attempts = _RECOVERY_MAX_ATTEMPTS + 1
                    worked = True
                    continue
                if singleton and not _proves_fresh_absence(run, run_nodes[0], now):
                    worked = (
                        _schedule_recovery_wait(
                            run,
                            "singleton recovery waits for a fresh exact "
                            "absence observation",
                            now,
                        )
                        or worked
                    )
                    continue
                try:
                    if singleton:
                        authority = _singleton_recovery_authority(
                            session,
                            run,
                            run_nodes[0],
                            now,
                            next_run_generation=run.run_generation + 1,
                            start_timeout_seconds=(
                                self._singleton_start_timeout_seconds
                            ),
                        )
                    else:
                        previous_run_generation = run.run_generation
                        previous_observations = tuple(
                            (
                                node.observed_run_generation,
                                node.observation_process_running,
                                node.observation_observed_at,
                                node.observation_endpoint_ready,
                            )
                            for node in run_nodes
                        )
                        run.run_generation += 1
                        run_plan["run_generation"] = run.run_generation
                        run.plan = run_plan_document(run_plan)
                        for node in run_nodes:
                            node.observed_run_generation = None
                            node.observation_process_running = None
                            node.observation_observed_at = None
                            node.observation_endpoint_ready = None
                        try:
                            authority = _recovery_authority(
                                session,
                                run,
                                now,
                                failed[0].rank,
                                stop_run_generation=previous_run_generation,
                            )
                        except _RecoveryDependencyPending:
                            # A pending wait must not outlive this generation
                            # bump: the next attempt still has to stop the
                            # exact Start payloads of the running generation.
                            run.run_generation = previous_run_generation
                            run_plan["run_generation"] = previous_run_generation
                            run.plan = run_plan_document(run_plan)
                            for node, observation in zip(
                                run_nodes, previous_observations
                            ):
                                (
                                    node.observed_run_generation,
                                    node.observation_process_running,
                                    node.observation_observed_at,
                                    node.observation_endpoint_ready,
                                ) = observation
                            raise
                    if authority is None:
                        authority = _unproven(
                            run.id,
                            _MISMATCH,
                            "accepted run has no automatic recovery authority",
                        )
                    if isinstance(authority, Residue):
                        _settle_unrecoverable(run, authority.note, now)
                        worked = True
                        continue
                    if singleton:
                        singleton_generation = run.run_generation
                        singleton_observation = (
                            run_nodes[0].observed_run_generation,
                            run_nodes[0].observation_process_running,
                            run_nodes[0].observation_observed_at,
                            run_nodes[0].observation_endpoint_ready,
                        )
                        run.run_generation += 1
                        run_plan["run_generation"] = run.run_generation
                        run.plan = run_plan_document(run_plan)
                        run_nodes[0].observed_run_generation = None
                        run_nodes[0].observation_process_running = None
                        run_nodes[0].observation_observed_at = None
                        run_nodes[0].observation_endpoint_ready = None
                    if singleton:
                        if self._recovery_run_stops is None:
                            unowned = _unproven(
                                run.id,
                                _MISSING,
                                "singleton recovery Stop owner is unavailable",
                            )
                            _settle_unrecoverable(run, unowned.note, now)
                            worked = True
                            continue
                        deadline = authority.get("deadline")
                        start_phases = authority.get("start_phases")
                        workload_intent_ordinal = authority.get(
                            "workload_intent_ordinal"
                        )
                        if (
                            not isinstance(deadline, str)
                            or not isinstance(start_phases, list)
                            or type(workload_intent_ordinal) is not int
                            or workload_intent_ordinal < 1
                        ):
                            malformed = _unproven(
                                run.id,
                                _DAMAGED,
                                "singleton recovery authority is invalid",
                            )
                            _settle_unrecoverable(run, malformed.note, now)
                            worked = True
                            continue
                        recovery_context = {
                            "schema_version": 1,
                            "failed_rank": 0,
                            "deadline": deadline,
                            "start_phases": _encode_phases(start_phases),
                        }
                        job = self._recovery_run_stops.queue_recovery_stop_in_session(
                            session,
                            run.id,
                            recovery_context=recovery_context,
                            workload_intent_ordinal=workload_intent_ordinal,
                            now=now,
                        )
                        if isinstance(job, Residue):
                            # The owner retired the damage as unknown: a scope
                            # that is not current yet is retried on the next
                            # pass, anything else is settled for this run only.
                            if job.reason is BookkeepingReason.EVIDENCE_UNAVAILABLE:
                                # Nothing was queued: the generation bump waits too.
                                run.run_generation = singleton_generation
                                run_plan["run_generation"] = singleton_generation
                                run.plan = run_plan_document(run_plan)
                                (
                                    run_nodes[0].observed_run_generation,
                                    run_nodes[0].observation_process_running,
                                    run_nodes[0].observation_observed_at,
                                    run_nodes[0].observation_endpoint_ready,
                                ) = singleton_observation
                            else:
                                _settle_unrecoverable(run, job.note, now)
                                worked = True
                            continue
                    else:
                        job = _enqueue_recovery_stop(
                            session,
                            self._agent_jobs,
                            run,
                            authority,
                            failed_rank=failed[0].rank,
                            now=now,
                        )
                        if isinstance(job, Residue):
                            _settle_unrecoverable(run, job.note, now)
                            worked = True
                            continue
                except _RecoveryDependencyPending as pending:
                    worked = _schedule_recovery_wait(run, str(pending), now) or worked
                    continue
                except DistributedLifecycleError as error:
                    _settle_unrecoverable(run, str(error), now)
                    worked = True
                    continue
                run.route_state = RouteState.WITHDRAWN
                run.route_error = f"distributed recovery queued: {job.id}"
                _advance_recovery_check(run, now)
                run.updated_at = now
                queued = True
                worked = True
                break
            worked = settle_observed_absent_runs_in_session(session, now) or worked
            worked = release_inactive_run_claims_in_session(session, now) or worked
        if queued:
            self._agent_jobs.notify_available()
        return worked

    def _withdraw_routes(self, now: datetime) -> bool:
        """Withdraw the routes of runs this pass may recover or settle."""

        with self._routes.publication_transaction() as session:
            candidates = {
                *_unreadable_run_ids(session),
                *session.scalars(
                    select(RecipeRun.id)
                    .where(
                        RecipeRun.state == RunState.RUNNING,
                        or_(
                            RecipeRun.route_next_attempt_at.is_(None),
                            RecipeRun.route_next_attempt_at <= now,
                        ),
                        select(RunNode.run_id)
                        .where(
                            RunNode.run_id == RecipeRun.id,
                            RunNode.state == RunState.FAILED,
                        )
                        .exists(),
                    )
                    .order_by(RecipeRun.created_at, RecipeRun.id)
                ),
            }
            pending = frozenset(
                run_id
                for run_id in sorted(candidates)
                if not self._routes.withdrawal_complete_in_session(
                    session, frozenset({run_id})
                )
            )
        if not pending:
            return False
        self._routes.withdraw_runs(pending, pending="recovery")
        return True

    def _settle_unreadable_runs(self, session: Session, now: datetime) -> bool:
        """A run whose stored plan this Controller cannot read is settled.

        Such a run was written under an older contract: it can be neither
        recovered nor stopped from its plan, so it must not keep holding
        capacity or wait for a stop that can never be planned. Its claims are
        released by the ownership rule below.
        """

        settled = False
        for run_id in _unreadable_run_ids(session):
            run = session.get(RecipeRun, run_id, with_for_update=True)
            if run is None or run.state not in STOPPABLE_RUN_STATES:
                continue
            if not self._routes.withdrawal_complete_in_session(
                session, frozenset({run.id})
            ):
                continue  # withdrawn first on the next tick
            _settle_unrecoverable(
                run,
                "the stored run plan is unreadable (older contract); the run is "
                "settled and its capacity released",
                now,
            )
            settled = True
        return settled

    @staticmethod
    def _active_recovery(session: Session, run_id: str) -> bool:
        jobs = session.scalars(
            select(Job).where(
                Job.kind.in_({"recipe.start", "recipe.stop"}),
                Job.state.in_({"queued", "running"}),
            )
        )
        return any(
            job.payload.get("owner_id") == run_id
            and isinstance(job.payload.get("recovery"), Mapping)
            for job in jobs
        )


def _unreadable_run_ids(session: Session) -> list[str]:
    unreadable = []
    for run in session.scalars(
        select(RecipeRun).where(RecipeRun.state.in_(STOPPABLE_RUN_STATES))
    ):
        try:
            run_plan_document(run.plan)
        except RecipeExecutionContractError:
            unreadable.append(run.id)
    return unreadable


def recovery_start_plan(
    payload: Mapping[str, object],
    *,
    now: datetime,
    require_unexpired: bool = True,
) -> (
    tuple[tuple[tuple[tuple[str, Mapping[str, object]], ...], ...], dict[str, object]]
    | None
):
    """Decode the trusted start phases carried by a recovery stop job."""

    value = payload.get("recovery")
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "failed_rank",
        "deadline",
        "start_phases",
    }:
        raise DistributedRecoveryInvalid(
            "distributed recovery authority is invalid",
            reason=InvalidRequestReason.MALFORMED,
        )
    enforce_recovery_deadline(payload, now=now, require_unexpired=require_unexpired)
    failed_rank = value["failed_rank"]
    deadline_value = value["deadline"]
    phases = _decode_phases(value.get("start_phases"))
    if phases is None:
        raise DistributedRecoveryInvalid(
            "distributed recovery phases are invalid",
            reason=InvalidRequestReason.MALFORMED,
        )
    marker = {
        "schema_version": 1,
        "failed_rank": failed_rank,
        "deadline": deadline_value,
    }
    return phases, marker


def enforce_recovery_deadline(
    payload: Mapping[str, object],
    *,
    now: datetime,
    require_unexpired: bool = True,
) -> bool:
    """Validate and enforce a retained recovery marker at a trust boundary."""

    value = payload.get("recovery")
    if value is None:
        return False
    if not isinstance(value, Mapping) or set(value) not in (
        {"schema_version", "failed_rank", "deadline"},
        {"schema_version", "failed_rank", "deadline", "start_phases"},
    ):
        raise DistributedRecoveryInvalid(
            "distributed recovery authority is invalid",
            reason=InvalidRequestReason.MALFORMED,
        )
    failed_rank = value.get("failed_rank")
    deadline_value = value.get("deadline")
    if (
        value.get("schema_version") != 1
        or type(failed_rank) is not int
        or failed_rank < 0
        or not isinstance(deadline_value, str)
    ):
        raise DistributedRecoveryInvalid(
            "distributed recovery authority is invalid",
            reason=InvalidRequestReason.MALFORMED,
        )
    try:
        deadline = datetime.fromisoformat(deadline_value)
    except ValueError as error:
        raise DistributedRecoveryInvalid(
            "distributed recovery authority is invalid",
            reason=InvalidRequestReason.MALFORMED,
        ) from error
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise DistributedRecoveryInvalid(
            "distributed recovery authority is invalid",
            reason=InvalidRequestReason.MALFORMED,
        )
    if require_unexpired and _aware(now) >= _aware(deadline):
        raise DistributedRecoveryInvalid(
            "distributed recovery deadline elapsed",
            reason=InvalidRequestReason.OUT_OF_RANGE,
        )
    return True


def _proves_fresh_absence(run: RecipeRun, node: RunNode, now: datetime) -> bool:
    """Require a current-generation observation that reports no process."""

    if (
        node.state != "failed"
        or node.observed_run_generation != run.run_generation
        or node.observation_process_running is not False
        or node.observation_observed_at is None
    ):
        return False
    try:
        run_plan = run_plan_document(run.plan)
    except RecipeExecutionContractError:
        return False
    run_nodes = run_plan.get("nodes")
    if (
        run_plan.get("observation_schema_version") != 2
        or run_plan.get("run_generation") != run.run_generation
        or run_plan.get("execution_mode") == "one-shot-jobs"
        or not isinstance(run_nodes, list)
        or len(run_nodes) != 1
        or not isinstance(run_nodes[0], Mapping)
        or run_nodes[0].get("node_id") != node.node_id
        or run_nodes[0].get("rank") != 0
        or run_nodes[0].get("role") != "entrypoint"
        or run_nodes[0].get("endpoint_owner") is not True
    ):
        return False
    if node.observation_endpoint_ready is not False or not isinstance(
        node.observation_observed_at, datetime
    ):
        return False
    observed_at = _aware(node.observation_observed_at)
    age = _aware(now) - observed_at
    return timedelta(0) <= age < timedelta(seconds=ROUTE_EVIDENCE_MAX_AGE_SECONDS)


def run_node_reports_absent(run: RecipeRun, node: RunNode, now: datetime) -> bool:
    """The Spark's own current-generation report says this rank is not running."""

    observed_at = node.observation_observed_at
    if (
        node.observed_run_generation != run.run_generation
        or node.observation_process_running is not False
        or not isinstance(observed_at, datetime)
    ):
        return False
    # Stored timestamps can come back naive (SQLite); they are UTC either way.
    stamp = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=UTC)
    age = now.astimezone(UTC) - stamp
    return timedelta(0) <= age < timedelta(seconds=ROUTE_EVIDENCE_MAX_AGE_SECONDS)


def _superseded(session: Session, run: RecipeRun, run_nodes: Sequence[RunNode]) -> bool:
    """A newer workload intent owns at least one of this run's Sparks.

    Recovery restarts only a run that is still desired; once a later intent
    has taken its Sparks there is nothing left to recover.
    """

    accepted = [
        ordinal
        for ordinal in session.scalars(
            select(Job.payload["workload_intent_ordinal"].as_integer()).where(
                Job.kind == "recipe.start",
                Job.payload["owner_kind"].as_string() == "run",
                Job.payload["owner_id"].as_string() == run.id,
                Job.state == "succeeded",
            )
        )
        if ordinal is not None
    ]
    if not accepted or not run_nodes:
        return False
    ordinal = min(accepted)
    return any(
        value > ordinal
        for value in session.scalars(
            select(AgentNode.workload_intent_ordinal).where(
                AgentNode.node_id.in_([node.node_id for node in run_nodes])
            )
        )
    )


def _settle_unrecoverable(run: RecipeRun, reason: str, now: datetime) -> None:
    """Record why a run is not recovered; the ownership rule frees its claims."""

    run.state = RunState.FAILED
    run.route_state = RouteState.WITHDRAWN
    run.route_error = reason[:512]
    run.route_next_attempt_at = None
    run.updated_at = now


def release_inactive_run_claims_in_session(session: Session, now: datetime) -> bool:
    """The one ownership rule for run capacity.

    Ports, rendezvous ports and memory are held only by a run a plan can still
    stop (planned, starting, running, stopping, lost): a load that needs its
    Sparks plans that Stop, which releases the claims. Any other run (failed,
    stopped, or missing) can never be stopped by a plan, so it holds nothing:
    whatever it may still occupy physically is what the Spark's next
    inventory reports, never a reservation that blocks admission forever.
    """

    claims = session.scalars(
        select(ResourceReservation)
        .outerjoin(RecipeRun, RecipeRun.id == ResourceReservation.owner_id)
        .where(
            ResourceReservation.owner_kind == "run",
            ResourceReservation.state == ReservationState.ACTIVE,
            or_(RecipeRun.id.is_(None), RecipeRun.state.not_in(STOPPABLE_RUN_STATES)),
        )
        .with_for_update(of=ResourceReservation)
    ).all()
    for claim in claims:
        claim.state = ReservationState.RELEASED
        claim.released_at = now
    return bool(claims)


def settle_observed_absent_runs_in_session(session: Session, now: datetime) -> bool:
    """Release the claims of a stoppable run every Spark reports absent.

    Unknown resident usage is resolved by observation, never held forever. A
    run that is not running (a cancelled start leaves it lost) and that no live
    operation owns, whose every rank's Spark has reported the process absent
    after the run last changed, occupies nothing the Spark does not already
    count in its inventory. It is recorded stopped and its claims are released.
    A run still running, starting or stopping under an operation, or any rank
    without a fresh absence report, keeps its claims: absence needs proof.
    """

    runs = session.scalars(
        select(RecipeRun)
        .where(
            RecipeRun.state.in_(STOPPABLE_NOT_RUNNING_RUN_STATES),
            RecipeRun.id.in_(
                select(ResourceReservation.owner_id).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.state == ReservationState.ACTIVE,
                )
            ),
        )
        .order_by(RecipeRun.created_at, RecipeRun.id)
        .with_for_update(of=RecipeRun, skip_locked=True)
    ).all()
    settled = False
    for run in runs:
        nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == run.id)
                .order_by(RunNode.rank, RunNode.node_id)
                .with_for_update(of=RunNode)
            )
        )
        if (
            not nodes
            or run_has_live_operation(session, run.id)
            or not all(run_node_reports_absent(run, node, now) for node in nodes)
        ):
            continue
        settle_absent_run_in_session(session, run, nodes, now)
        settled = True
    return settled


def settle_absent_run_in_session(
    session: Session,
    run: RecipeRun,
    run_nodes: Sequence[RunNode],
    now: datetime,
) -> None:
    """Record a run its Sparks report gone as stopped and release its claims."""

    for node in run_nodes:
        node.state = RunState.STOPPED
        node.updated_at = now
    run.state = RunState.STOPPED
    run.route_state = RouteState.WITHDRAWN
    run.route_error = None
    run.route_next_attempt_at = None
    run.stopped_at = now
    run.updated_at = now
    release_owned_reservations_in_session(session, "run", run.id, now)


def _advance_recovery_check(run: RecipeRun, now: datetime) -> None:
    run.recovery_attempts += 1
    backoff = min(
        _RECOVERY_RECHECK_SECONDS * 2 ** (run.recovery_attempts - 1),
        _RECOVERY_COOLDOWN_SECONDS,
    )
    run.route_next_attempt_at = now + timedelta(seconds=backoff)


def _schedule_recovery_wait(run: RecipeRun, reason: str, now: datetime) -> bool:
    """Persist a bounded check time without turning an unchanged wait into work."""

    changed = False
    if run.route_error != reason:
        run.route_error = reason[:512]
        changed = True
    next_attempt = run.route_next_attempt_at
    due = (
        next_attempt
        if next_attempt is None or next_attempt.tzinfo is not None
        else next_attempt.replace(tzinfo=UTC)
    )
    if due is None or due <= now:
        _advance_recovery_check(run, now)
        changed = True
    if changed:
        run.updated_at = now
    return changed


def _singleton_recovery_authority(
    session: Session,
    run: RecipeRun,
    run_node: RunNode,
    now: datetime,
    *,
    next_run_generation: int,
    start_timeout_seconds: int,
) -> dict[str, object] | Residue | None:
    """Rebuild one exact accepted persistent Start after observed absence."""

    if not _proves_fresh_absence(run, run_node, now):
        return None
    installation = session.get(RecipeInstallation, run.installation_id)
    resolved = (
        _active_recipe_revision(session, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    if (
        installation is None
        or installation.state != InstallationState.INSTALLED
        or installation.image_digest is None
        or resolved is None
    ):
        return _unproven(
            run.id, _MISSING, "singleton recovery installation authority is missing"
        )
    revision, recipe = resolved
    if recipe.topology.distributed:
        return None
    nodes = tuple(
        session.scalars(
            select(RunNode).where(RunNode.run_id == run.id).order_by(RunNode.rank)
        )
    )
    if (
        len(nodes) != 1
        or nodes[0].id != run_node.id
        or run_node.rank != 0
        or run_node.role != "entrypoint"
    ):
        return _unproven(run.id, _DAMAGED, "singleton recovery rank set is invalid")
    try:
        run_plan = run_plan_document(run.plan)
        installation_plan = installation_plan_document(installation.plan)
    except RecipeExecutionContractError as error:
        return _unproven(run.id, _DAMAGED, "singleton recovery plan is invalid", error)
    run_nodes = run_plan.get("nodes")
    compiled_plans = installation_plan.get("compiled_execution_plans")
    if (
        run.installation_id != installation.id
        or run.mapping_id != installation.mapping_id
        or run.mapping_generation != installation.mapping_generation
        or run_plan.get("installation_id") != installation.id
        or run_plan.get("mapping_id") != run.mapping_id
        or run_plan.get("mapping_generation") != run.mapping_generation
        or run_plan.get("recipe_revision_id") != revision.id
        or run_plan.get("plan_digest") != run.plan_digest
        or run_plan.get("alias") != run.alias
        or run_plan.get("run_generation") != run.run_generation
        or run_plan.get("execution_mode") == "one-shot-jobs"
        or installation_plan.get("mapping_id") != installation.mapping_id
        or installation_plan.get("mapping_generation")
        != installation.mapping_generation
        or installation_plan.get("recipe_revision_id") != revision.id
        or installation_plan.get("recipe_content_sha256") != revision.content_digest
        or installation_plan.get("image_digest") != installation.image_digest
        or not isinstance(run_nodes, list)
        or len(run_nodes) != 1
        or not isinstance(run_nodes[0], Mapping)
        or not isinstance(compiled_plans, Mapping)
        or set(compiled_plans) != {run_node.node_id}
    ):
        return _unproven(
            run.id, _MISMATCH, "singleton recovery plan authority is stale"
        )
    plan_node = run_nodes[0]
    install_compiled_plan = compiled_plans.get(run_node.node_id)
    if (
        plan_node.get("node_id") != run_node.node_id
        or plan_node.get("rank") != run_node.rank
        or plan_node.get("role") != run_node.role
        or plan_node.get("port") != run_node.port
        or plan_node.get("required_memory_bytes") != run_node.reserved_memory_bytes
        or plan_node.get("endpoint_owner") is not True
        or plan_node.get("fabric_address") is not None
        or not isinstance(install_compiled_plan, Mapping)
    ):
        return _unproven(run.id, _DAMAGED, "singleton recovery placement is invalid")
    memory_floor = plan_node.get("memory_floor_bytes")
    memory_kind = plan_node.get("memory_kind")
    if (
        type(memory_floor) is not int
        or memory_floor < 0
        or memory_kind not in {"unified", "host", "accelerator"}
    ):
        return _unproven(run.id, _DAMAGED, "singleton recovery resources are invalid")
    mapping = session.get(ClusterMapping, run.mapping_id)
    if (
        mapping is None
        or mapping.state != "ready"
        or mapping.generation != run.mapping_generation
        or mapping.endpoint_owner_node_id != run_node.node_id
    ):
        return _unproven(run.id, _MISMATCH, "singleton recovery mapping is stale")
    stop_timeout = recipe.runtime.lifecycle.stop_timeout_seconds
    start_timeout = validate_distributed_start_timeout_seconds(start_timeout_seconds)
    deadline = _aware(now) + timedelta(seconds=start_timeout + stop_timeout)
    agent_node = session.get(AgentNode, run_node.node_id)
    if agent_node is None or agent_node.state != "active":
        return _unproven(
            run.id,
            _MISSING if agent_node is None else _MISMATCH,
            "singleton recovery requires exact Stop and observation support",
        )
    presence = session.scalar(
        select(AgentPresence)
        .where(AgentPresence.node_id == run_node.node_id)
        .order_by(AgentPresence.observed_at.desc())
        .limit(1)
    )
    if (
        presence is None
        or not isinstance(presence.management_address, str)
        or not timedelta(0)
        <= _aware(now) - _aware(presence.observed_at)
        < timedelta(seconds=ROUTE_EVIDENCE_MAX_AGE_SECONDS)
    ):
        raise _RecoveryDependencyPending(
            "singleton recovery waits for a fresh Controller-observed Spark presence report",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    accepted = _accepted_start_authority(
        session, run, revision.content_digest, run_node.node_id
    )
    if isinstance(accepted, Residue):
        return accepted
    start_job, start_ordinal = accepted
    accepted_start_payload = _accepted_start_authority_payload(
        session, start_job, run_node.node_id
    )
    if isinstance(accepted_start_payload, Residue):
        return accepted_start_payload
    try:
        accepted_start = read_stored_model(
            RecipeStartPayload,
            canonical_message(accepted_start_payload),
            from_json=True,
        )
    except (TypeError, ValueError) as error:
        return _unproven(run.id, _DAMAGED, "accepted Start payload is invalid", error)
    compiled_plan = accepted_start_payload.get("compiled_execution_plan")
    if not isinstance(compiled_plan, Mapping):
        return _unproven(run.id, _DAMAGED, "accepted Start plan is invalid")
    expected_lifecycle = {"stop_timeout_seconds": stop_timeout}
    for label, plan in (
        ("installed", install_compiled_plan),
        ("accepted Start", compiled_plan),
    ):
        compiled_lifecycle = plan.get("lifecycle")
        if (
            not isinstance(compiled_lifecycle, Mapping)
            or set(compiled_lifecycle) != set(expected_lifecycle)
            or canonical_message(dict(compiled_lifecycle))
            != canonical_message(expected_lifecycle)
        ):
            return _unproven(
                run.id,
                _MISMATCH,
                f"singleton recovery {label} lifecycle differs from accepted recipe",
            )
    accepted_compiled_identity = compiled_plan.get("identity")
    install_compiled_identity = install_compiled_plan.get("identity")
    accepted_placement = accepted_start.compiled_execution_plan.runtime.placement
    if (
        str(accepted_start.run_id) != run.id
        or str(accepted_start.installation_id) != installation.id
        or str(accepted_start.recipe_revision_id) != revision.id
        or str(accepted_start.mapping_id) != run.mapping_id
        or accepted_start.run_generation != run.run_generation
        # The Job-level binding selects the Start; the exact per-node payload
        # that recovery replays must name the same run plan.
        or accepted_start.plan_digest != run.plan_digest
        or (
            accepted_placement.rank,
            accepted_placement.role,
            accepted_placement.port,
            accepted_placement.reserved_memory_bytes,
            accepted_placement.memory_floor_bytes,
            accepted_placement.world_size,
        )
        != (
            run_node.rank,
            run_node.role,
            run_node.port,
            run_node.reserved_memory_bytes,
            memory_floor,
            1,
        )
        or accepted_start.phase is not None
        or not isinstance(accepted_compiled_identity, Mapping)
        or not isinstance(install_compiled_identity, Mapping)
        or canonical_message(accepted_compiled_identity)
        != canonical_message(install_compiled_identity)
    ):
        return _unproven(run.id, _MISMATCH, "accepted Start image authority is stale")
    if start_job.result is not None and (
        not isinstance(start_job.result, Mapping)
        or start_job.result.get("cancel_requested") is True
    ):
        return _unproven(
            run.id, _MISMATCH, "singleton recovery start authority was cancelled"
        )
    try:
        start_payload = build_recipe_start_payload(
            run_id=run.id,
            installation_id=installation.id,
            recipe_revision_id=revision.id,
            mapping_id=run.mapping_id,
            run_generation=next_run_generation,
            plan_digest=run.plan_digest,
            placement=RecipeStartPlacement(
                run_node.node_id,
                run_node.rank,
                run_node.role,
                run_node.port,
                run_node.reserved_memory_bytes,
                memory_floor,
                memory_kind,
                None,
            ),
            compiled_endpoint_address=presence.management_address,
            world_size=1,
            compiled_execution_plan=WireCompiledExecutionPlan.parse(compiled_plan),
            master_address=None,
            master_port=None,
        )
    except (KeyError, RecipeStartPayloadError) as error:
        return _unproven(
            run.id, _DAMAGED, "singleton recovery start payload is invalid", error
        )
    return {
        "deadline": deadline.isoformat(),
        "workload_intent_ordinal": start_ordinal,
        "failed_rank": 0,
        "recipe_content_sha256": revision.content_digest,
        "start_phases": [[(run_node.node_id, start_payload)]],
    }


def _accepted_start_authority(
    session: Session,
    run: RecipeRun,
    recipe_digest: str,
    node_id: str,
) -> tuple[Job, int] | Residue:
    starts = tuple(
        session.scalars(
            select(Job)
            .where(
                Job.kind == "recipe.start",
                Job.payload["owner_kind"].as_string() == "run",
                Job.payload["owner_id"].as_string() == run.id,
            )
            .order_by(Job.created_at, Job.id)
        )
    )
    accepted = tuple(
        job
        for job in starts
        if job.payload.get("recovery") is None
        and job.state == "succeeded"
        and _start_binds_current_run_plan(session, job, run)
    )
    original = accepted[-1] if accepted else None
    targets = sorted(
        session.scalars(select(RunNode.node_id).where(RunNode.run_id == run.id))
    )
    ordinal = original.payload.get("workload_intent_ordinal") if original else None
    if (
        original is None
        or original.payload.get("schema_version") != 1
        or original.payload.get("owner_kind") != "run"
        or original.payload.get("owner_id") != run.id
        or original.targets != targets
        or type(ordinal) is not int
        or ordinal < 1
    ):
        return _unproven(
            run.id, _MISSING, "singleton recovery lacks exact accepted Start authority"
        )
    if run.run_generation == 1:
        start = original
    else:
        current = tuple(
            job
            for job in starts
            if isinstance(job.payload.get("recovery"), Mapping)
            and job.state == "succeeded"
            and _start_binds_current_run_plan(session, job, run)
            and _launch_generation(session, job, node_id) == run.run_generation
        )
        start = current[-1] if current else None
        if start is None:
            return _unproven(
                run.id,
                _MISSING,
                "singleton recovery lacks current-generation Start authority",
            )
        origin = _validate_singleton_recovery_start_origin(
            session,
            start,
            run=run,
            recipe_digest=recipe_digest,
            targets=targets,
            workload_intent_ordinal=ordinal,
        )
        if isinstance(origin, Residue):
            return origin
    if (
        start.payload.get("schema_version") != 1
        or start.payload.get("owner_kind") != "run"
        or start.payload.get("owner_id") != run.id
        or start.targets != targets
        or start.payload.get("workload_intent_ordinal") != ordinal
        or (
            isinstance(start.result, Mapping)
            and start.result.get("cancel_requested") is True
        )
    ):
        return _unproven(
            run.id, _MISSING, "singleton recovery lacks exact current Start authority"
        )
    payload = _accepted_start_authority_payload(session, start, node_id)
    if isinstance(payload, Residue):
        return payload
    return start, ordinal


def _launch_generation(session: Session, job: Job, node_id: str) -> int | None:
    """The run generation the node's fenced Start of this job launched."""
    payload = session.scalar(
        select(AgentOperation.payload)
        .where(
            AgentOperation.parent_job_id == job.id,
            AgentOperation.node_id == node_id,
            AgentOperation.state == "succeeded",
        )
        .order_by(AgentOperation.created_at.desc(), AgentOperation.id.desc())
        .limit(1)
    )
    generation = payload.get("run_generation") if isinstance(payload, Mapping) else None
    return generation if type(generation) is int else None


def _validate_singleton_recovery_start_origin(
    session: Session,
    start: Job,
    *,
    run: RecipeRun,
    recipe_digest: str,
    targets: list[str],
    workload_intent_ordinal: int,
) -> Residue | None:
    marker = start.payload.get("recovery")
    deadline = marker.get("deadline") if isinstance(marker, Mapping) else None
    if (
        not isinstance(marker, Mapping)
        or set(marker) != {"schema_version", "failed_rank", "deadline"}
        or marker.get("schema_version") != 1
        or marker.get("failed_rank") != 0
        or not isinstance(deadline, str)
        or start.payload.get("workload_intent_ordinal") != workload_intent_ordinal
    ):
        return _unproven(run.id, _DAMAGED, "singleton recovery Start marker is invalid")
    stop_request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:singleton-recovery-stop:{run.id}:{run.run_generation}:{deadline}",
        )
    )
    stops = tuple(
        session.scalars(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.request_id == stop_request_id,
                Job.payload["owner_kind"].as_string() == "run",
                Job.payload["owner_id"].as_string() == run.id,
            )
        )
    )
    stop = stops[0] if len(stops) == 1 else None
    if stop is None:
        return _unproven(
            run.id, _MISSING, "singleton recovery Start lacks its exact completed Stop"
        )
    recovery = stop.payload.get("recovery")
    if stop.state != "succeeded" or stop.actor != "system:singleton-recovery":
        return _unproven(
            run.id,
            _MISMATCH,
            "singleton recovery Stop did not complete under its recovery actor",
        )
    if (
        stop.authority_revision != run.plan_digest
        or stop.targets != targets
        or stop.payload.get("workload_intent_ordinal") != workload_intent_ordinal
        or stop.payload_digest
        != hashlib.sha256(canonical_message(stop.payload)).hexdigest()
    ):
        return _unproven(
            run.id, _MISMATCH, "singleton recovery Stop has stale exact run authority"
        )
    if (
        not isinstance(recovery, Mapping)
        or set(recovery)
        != {"schema_version", "failed_rank", "deadline", "start_phases"}
        or any(recovery.get(key) != marker.get(key) for key in marker)
    ):
        return _unproven(
            run.id,
            _MISMATCH,
            "singleton recovery Stop continuation differs from its Start",
        )
    phases = _decode_phases(recovery.get("start_phases"))
    if phases is None:
        return _unproven(
            run.id, _DAMAGED, "singleton recovery Stop continuation is invalid"
        )
    projected_start_phases = _project_start_phases(start.payload.get("phases"))
    if (
        projected_start_phases is None
        or canonical_message(_encode_phases(phases))
        != canonical_message(projected_start_phases)
        or start.request_id
        != str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"vonk:distributed-recovery-start:{stop.id}",
            )
        )
    ):
        return _unproven(
            run.id,
            _MISMATCH,
            "singleton recovery Start differs from its exact Stop continuation",
        )
    return None


def _start_binds_current_run_plan(session: Session, start: Job, run: RecipeRun) -> bool:
    """Only completed Start receipts for the run's current exact plan can seed recovery."""

    try:
        plan = run_plan_document(run.plan)
    except RecipeExecutionContractError:
        return False
    installation = session.get(RecipeInstallation, run.installation_id)
    revision = (
        session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    return (
        start.state == "succeeded"
        and revision is not None
        and start.authority_revision == revision.content_digest
        and start.payload.get("plan_digest") == run.plan_digest
        and plan.get("plan_digest") == run.plan_digest
        and start.payload_digest
        == hashlib.sha256(canonical_message(start.payload)).hexdigest()
    )


def _project_start_phases(value: object) -> list[list[dict[str, object]]] | None:
    if not isinstance(value, list) or not value:
        return None
    projected: list[list[dict[str, object]]] = []
    for raw_group in value:
        if not isinstance(raw_group, list) or not raw_group:
            return None
        group: list[dict[str, object]] = []
        for raw_item in raw_group:
            if (
                not isinstance(raw_item, Mapping)
                or not isinstance(raw_item.get("node_id"), str)
                or not isinstance(raw_item.get("payload"), Mapping)
            ):
                return None
            group.append(
                {
                    "node_id": raw_item["node_id"],
                    "payload": dict(raw_item["payload"]),
                }
            )
        projected.append(group)
    return projected


def _accepted_start_authority_payload(
    session: Session, start: Job, node_id: str
) -> Mapping[str, object] | Residue:
    """The exact payload of the one accepted singleton Start child of ``node_id``."""

    phases = start.payload.get("phases")
    if not isinstance(phases, list) or not phases:
        return _unproven(start.id, _MISSING, "accepted Start payload is missing")
    if sum(len(phase) for phase in phases if isinstance(phase, list)) != 1:
        return _unproven(start.id, _DAMAGED, "accepted Start payload is not singleton")
    items = [
        item
        for phase in phases
        if isinstance(phase, list)
        for item in phase
        if isinstance(item, Mapping) and item.get("node_id") == node_id
    ]
    if len(items) != 1:
        return _unproven(start.id, _DAMAGED, "accepted Start payload is not singleton")
    child = _accepted_start_child(session, start, node_id, items[0])
    if isinstance(child, Residue):
        return child
    return dict(child.payload)


def _accepted_start_child(
    session: Session,
    start: Job,
    node_id: str,
    item: Mapping[str, object],
) -> AgentOperation | Residue:
    operation_id = item.get("operation_id")
    payload = item.get("payload")
    if not isinstance(operation_id, str) or not isinstance(payload, Mapping):
        return _unproven(start.id, _DAMAGED, "accepted Start child identity is invalid")
    child = session.get(AgentOperation, operation_id)
    if (
        child is None
        or child.parent_job_id != start.id
        or child.node_id != node_id
        or child.kind != "recipe.start"
        or child.state != "succeeded"
        or not isinstance(child.payload, Mapping)
        or canonical_message(child.payload) != canonical_message(payload)
        or child.payload_digest
        != hashlib.sha256(canonical_message(child.payload)).hexdigest()
    ):
        return _unproven(
            start.id, _MISMATCH, "accepted Start payload binding is invalid"
        )
    return child


def _recovery_authority(
    session: Session,
    run: RecipeRun,
    now: datetime,
    failed_rank: int,
    *,
    stop_run_generation: int,
) -> dict[str, object] | Residue | None:
    installation = session.get(RecipeInstallation, run.installation_id)
    resolved = (
        _active_recipe_revision(session, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    if installation is None or resolved is None or installation.image_digest is None:
        return _unproven(run.id, _MISSING, "distributed recovery authority is missing")
    revision, recipe = resolved
    topology = recipe.topology
    # A distributed topology withdraws its endpoint on rank loss and recovers
    # by restarting the workers and then the entrypoint.
    readiness = canonical_distributed_readiness(
        topology=topology,
        interfaces=[
            interface.model_dump(mode="json") for interface in recipe.interfaces
        ],
    )
    if readiness is None:
        return None
    stop_timeout = recipe.runtime.lifecycle.stop_timeout_seconds
    nodes = tuple(
        session.scalars(
            select(RunNode).where(RunNode.run_id == run.id).order_by(RunNode.rank)
        )
    )
    if (
        tuple(node.rank for node in nodes) != tuple(range(len(nodes)))
        or len(nodes) != topology.node_count
        or failed_rank not in {node.rank for node in nodes}
    ):
        return _unproven(run.id, _DAMAGED, "distributed recovery rank set is invalid")
    try:
        run_plan = run_plan_document(run.plan)
        installation_plan = installation_plan_document(installation.plan)
    except RecipeExecutionContractError as error:
        return _unproven(
            run.id, _DAMAGED, "distributed recovery plan is invalid", error
        )
    if (
        run.installation_id != installation.id
        or run.mapping_id != installation.mapping_id
        or run.mapping_generation != installation.mapping_generation
        or run_plan is None
        or run_plan.get("installation_id") != run.installation_id
        or run_plan.get("mapping_id") != run.mapping_id
        or run_plan.get("mapping_generation") != run.mapping_generation
        or run_plan.get("recipe_revision_id") != installation.recipe_revision_id
        or run_plan.get("plan_digest") != run.plan_digest
        or run_plan.get("alias") != run.alias
        or run_plan.get("run_generation") != run.run_generation
    ):
        return _unproven(
            run.id, _MISMATCH, "distributed recovery run authority is stale"
        )
    plans = run_plan.get("nodes")
    compiled_plans = installation_plan.get("compiled_execution_plans")
    if (
        not isinstance(plans, list)
        or len(plans) != len(nodes)
        or not isinstance(compiled_plans, Mapping)
    ):
        return _unproven(run.id, _DAMAGED, "distributed recovery plan is invalid")
    by_rank = {item.get("rank"): item for item in plans if isinstance(item, Mapping)}
    owners = tuple(
        item
        for item in plans
        if isinstance(item, Mapping) and item.get("endpoint_owner") is True
    )
    if (
        len(by_rank) != len(nodes)
        or len(owners) != 1
        or set(compiled_plans) != {node.node_id for node in nodes}
    ):
        return _unproven(run.id, _DAMAGED, "distributed recovery plan is invalid")
    owner = owners[0]
    master_address = owner.get("fabric_address")
    master_port = owner.get("rendezvous_port")
    if not isinstance(master_address, str) or type(master_port) is not int:
        return _unproven(run.id, _DAMAGED, "distributed recovery rendezvous is invalid")
    presences: dict[str, str] = {}
    for node in nodes:
        presence = session.scalar(
            select(AgentPresence)
            .where(AgentPresence.node_id == node.node_id)
            .order_by(AgentPresence.observed_at.desc())
            .limit(1)
        )
        observed_at = None if presence is None else presence.observed_at
        # Stored timestamps can come back naive (SQLite); they are UTC either
        # way, matching the other stored-observation readers in this module.
        if observed_at is not None and observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        if (
            presence is None
            or not isinstance(presence.management_address, str)
            or observed_at is None
            or not timedelta(0)
            <= _aware(now) - observed_at
            < timedelta(seconds=ROUTE_EVIDENCE_MAX_AGE_SECONDS)
        ):
            raise _RecoveryDependencyPending(
                "distributed recovery waits for a fresh Controller-observed "
                "Spark presence report",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        presences[node.node_id] = presence.management_address
    original = _original_start_authority(session, run, revision.content_digest)
    if isinstance(original, Residue):
        return original
    start_job, startup_budget = original
    # Rank reload/JIT needs its accepted startup duration, not the short health
    # probe timeout. Ordered stops have their own per-role execution budget.
    # Persist the total once: phase advances, retries and route publication all
    # retain this exact deadline instead of granting time again after each stop.
    stop_order = topology.stop_order
    start_deadline = (
        now + startup_budget + timedelta(seconds=stop_timeout * len(stop_order))
    ).isoformat()
    start_payloads: dict[str, tuple[str, dict[str, object]]] = {}
    for node in nodes:
        plan = by_rank[node.rank]
        compiled_plan = compiled_plans.get(node.node_id)
        local_address = plan.get("fabric_address")
        endpoint_owner = plan.get("endpoint_owner")
        memory_floor = plan.get("memory_floor_bytes")
        memory_kind = plan.get("memory_kind")
        if (
            plan.get("node_id") != node.node_id
            or plan.get("rank") != node.rank
            or plan.get("role") != node.role
            or plan.get("port") != node.port
            or plan.get("required_memory_bytes") != node.reserved_memory_bytes
            or type(memory_floor) is not int
            or memory_floor < 0
            or memory_kind not in {"unified", "host", "accelerator"}
            or not isinstance(local_address, str)
            or type(endpoint_owner) is not bool
            or not isinstance(compiled_plan, Mapping)
        ):
            return _unproven(run.id, _DAMAGED, "distributed recovery plan is invalid")
        try:
            payload = build_recipe_start_payload(
                run_id=run.id,
                installation_id=installation.id,
                recipe_revision_id=revision.id,
                mapping_id=run.mapping_id,
                run_generation=run.run_generation,
                plan_digest=run.plan_digest,
                placement=RecipeStartPlacement(
                    node.node_id,
                    node.rank,
                    node.role,
                    node.port,
                    node.reserved_memory_bytes,
                    memory_floor,
                    memory_kind,
                    local_address,
                ),
                compiled_endpoint_address=(
                    presences[node.node_id] if endpoint_owner else None
                ),
                world_size=len(nodes),
                compiled_execution_plan=WireCompiledExecutionPlan.parse(compiled_plan),
                master_address=master_address,
                master_port=master_port,
                phase="rank-launch",
                start_deadline=start_deadline,
            )
        except (KeyError, RecipeStartPayloadError) as error:
            return _unproven(
                run.id, _DAMAGED, "distributed recovery start payload is invalid", error
            )
        start_payloads[node.role] = (node.node_id, payload)
    start_order = topology.start_order
    roles = {node.role for node in nodes}
    if (
        set(start_order) != roles
        or set(stop_order) != roles
        or len(start_order) != len(roles)
        or len(stop_order) != len(roles)
    ):
        return _unproven(run.id, _DAMAGED, "distributed recovery order is invalid")
    owner_role = owner.get("role")
    if not isinstance(owner_role, str) or owner_role not in start_payloads:
        return _unproven(run.id, _DAMAGED, "distributed recovery endpoint is invalid")
    owner_node_id, owner_payload = start_payloads[owner_role]
    if type(stop_run_generation) is not int or stop_run_generation < 1:
        return _unproven(
            run.id, _DAMAGED, "distributed recovery prior Start generation is invalid"
        )
    try:
        exact_stop_payloads = durable_run_stop_payloads(
            session,
            run,
            nodes,
            run_generation=stop_run_generation,
            cancel_pending_start=True,
            allow_missing_nodes=False,
        )
    except RecipeStopAuthorityError as error:
        return _unproven(
            run.id,
            _MISSING,
            "distributed recovery lacks exact prior Start Stop authority",
            error,
        )
    return {
        "deadline": start_deadline,
        "workload_intent_ordinal": start_job.payload.get("workload_intent_ordinal"),
        "failed_rank": failed_rank,
        "recipe_content_sha256": revision.content_digest,
        "start_phases": [
            [start_payloads[str(role)] for role in start_order],
            [
                (
                    owner_node_id,
                    {**owner_payload, "phase": "collective-readiness"},
                )
            ],
        ],
        "stop_phases": [
            [
                (
                    node.node_id,
                    json.loads(canonical_message(exact_stop_payloads[node.node_id])),
                )
                for node in nodes
                if node.role == role
            ]
            for role in stop_order
        ],
    }


def _enqueue_recovery_stop(
    session: Session,
    queue: _RecoveryJobQueue,
    run: RecipeRun,
    authority: Mapping[str, object],
    *,
    failed_rank: int,
    now: datetime,
) -> Job | Residue:
    raw_stop_phases = authority.get("stop_phases")
    raw_start_phases = authority.get("start_phases")
    recipe_digest = authority.get("recipe_content_sha256")
    deadline = authority.get("deadline")
    if (
        not isinstance(raw_stop_phases, list)
        or not isinstance(raw_start_phases, list)
        or not isinstance(recipe_digest, str)
        or not isinstance(deadline, str)
    ):
        return _unproven(run.id, _DAMAGED, "distributed recovery authority is invalid")
    stop_phases = tuple(tuple(group) for group in raw_stop_phases)
    start_phases = tuple(tuple(group) for group in raw_start_phases)
    request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:distributed-recovery:{run.id}:{failed_rank}:{deadline}",
        )
    )
    queued = session.scalar(select(Job).where(Job.request_id == request_id))
    if queued is not None:
        # The request identity is the deterministic recovery of this run, failed
        # rank and deadline: a repeat is the same request, answered with its Job.
        return queued
    job_id = str(uuid.uuid4())
    stop_phase_operations = tuple(
        tuple((str(uuid.uuid4()), node_id, payload) for node_id, payload in group)
        for group in stop_phases
    )
    job_payload = {
        "schema_version": 1,
        "owner_kind": "run",
        "owner_id": run.id,
        "plan_digest": run.plan_digest,
        "phases": [
            [
                {
                    "operation_id": operation_id,
                    "node_id": node_id,
                    "payload": json.loads(canonical_message(payload)),
                }
                for operation_id, node_id, payload in group
            ]
            for group in stop_phase_operations
        ],
        "recovery": {
            "schema_version": 1,
            "failed_rank": failed_rank,
            "deadline": deadline,
            "start_phases": _encode_phases(start_phases),
        },
    }
    targets = sorted(node_id for group in stop_phases for node_id, _payload in group)
    start_ordinal = authority.get("workload_intent_ordinal")
    target_nodes = tuple(
        session.scalars(
            select(AgentNode)
            .where(AgentNode.node_id.in_(targets))
            .order_by(AgentNode.node_id)
            .with_for_update(of=AgentNode)
        )
    )
    if (
        type(start_ordinal) is not int
        or start_ordinal < 1
        or tuple(node.node_id for node in target_nodes) != tuple(targets)
        or any(node.workload_intent_ordinal != start_ordinal for node in target_nodes)
    ):
        # A newer workload intent owns these Sparks: newer intent wins, so the
        # recorded Start is not replayed over it.
        return _unproven(
            run.id, _MISMATCH, "distributed recovery start authority was superseded"
        )
    job_payload["workload_intent_ordinal"] = start_ordinal
    job = JobAdapter.new_job(
        state="running",
        id=job_id,
        request_id=request_id,
        kind="recipe.stop",
        actor="system:distributed-recovery",
        authority_revision=run.plan_digest.removeprefix("sha256:"),
        targets=targets,
        payload_digest=hashlib.sha256(canonical_message(job_payload)).hexdigest(),
        payload=job_payload,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    for operation_id, node_id, payload in stop_phase_operations[0]:
        queue.enqueue_in_session(
            session,
            job.id,
            node_id,
            "recipe.stop",
            run.plan_digest.removeprefix("sha256:"),
            payload,
            operation_id=operation_id,
        )
    return job


def _original_start_authority(
    session: Session, run: RecipeRun, recipe_digest: str | None
) -> tuple[Job, timedelta] | Residue:
    """Read the exact accepted start's budget; configuration is not a fallback."""

    starts = tuple(
        session.scalars(
            select(Job)
            .where(
                Job.kind == "recipe.start",
                Job.payload["owner_kind"].as_string() == "run",
                Job.payload["owner_id"].as_string() == run.id,
                Job.payload["recovery"].as_string().is_(None),
            )
            .order_by(Job.created_at, Job.id)
        )
    )
    starts = tuple(
        start
        for start in starts
        if start.state == "succeeded"
        and _start_binds_current_run_plan(session, start, run)
    )
    if not starts:
        return _unproven(
            run.id, _MISSING, "distributed recovery lacks its start authority"
        )
    start = starts[-1]
    deadline_value = start.payload.get("start_deadline")
    ordinal = start.payload.get("workload_intent_ordinal")
    targets = sorted(
        session.scalars(select(RunNode.node_id).where(RunNode.run_id == run.id))
    )
    if (
        type(ordinal) is not int
        or ordinal < 1
        or start.targets != targets
        or not isinstance(deadline_value, str)
    ):
        return _unproven(
            run.id, _DAMAGED, "distributed recovery start authority is invalid"
        )
    try:
        parsed_deadline = datetime.fromisoformat(deadline_value)
        if parsed_deadline.utcoffset() is None:
            # A stored deadline without a zone is damaged bookkeeping, not a
            # clock fault.
            return _unproven(
                run.id, _DAMAGED, "distributed recovery start authority is invalid"
            )
        deadline = _aware(parsed_deadline)
        # PostgreSQL returns an aware UTC value; SQLite's test adapter drops
        # its timezone from this database-owned timestamp.
        created = start.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        duration = deadline - _aware(created)
        seconds = duration.total_seconds()
        if not seconds.is_integer():
            return _unproven(
                run.id, _DAMAGED, "distributed recovery start authority is invalid"
            )
        validate_distributed_start_timeout_seconds(int(seconds))
    except ValueError as error:
        return _unproven(
            run.id, _DAMAGED, "distributed recovery start authority is invalid", error
        )
    return start, duration


def _encode_phases(
    phases: Sequence[Sequence[tuple[str, Mapping[str, object]]]],
) -> list[list[dict[str, object]]]:
    return [
        [
            {"node_id": node_id, "payload": json.loads(canonical_message(payload))}
            for node_id, payload in group
        ]
        for group in phases
    ]


def _decode_phases(
    value: object,
) -> tuple[tuple[tuple[str, Mapping[str, object]], ...], ...] | None:
    """The stored start phases, or ``None`` when the stored value is damaged."""

    if not isinstance(value, list) or not value:
        return None
    phases: list[tuple[tuple[str, Mapping[str, object]], ...]] = []
    for raw_group in value:
        if not isinstance(raw_group, list) or not raw_group:
            return None
        group: list[tuple[str, Mapping[str, object]]] = []
        for item in raw_group:
            if not isinstance(item, Mapping) or set(item) != {"node_id", "payload"}:
                return None
            node_id = item.get("node_id")
            item_payload = item.get("payload")
            if not isinstance(node_id, str) or not isinstance(item_payload, Mapping):
                return None
            group.append((node_id, dict(item_payload)))
        phases.append(tuple(group))
    return tuple(phases)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DistributedRecoveryInvalid(
            "distributed recovery clock is invalid",
            reason=InvalidRequestReason.MALFORMED,
        )
    return value.astimezone(UTC)


__all__ = [
    "DistributedRecoveryCoordinator",
    "enforce_recovery_deadline",
    "recovery_start_plan",
]
