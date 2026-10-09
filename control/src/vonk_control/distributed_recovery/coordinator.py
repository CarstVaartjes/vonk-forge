"""Distributed recovery: coordinator concerns."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import OperatorActionName, RouteState, RunState

from ..distributed_lifecycle import DistributedLifecycleError
from ..job_documents import DistributedRecoveryMarker
from ..lifecycle.evidence import BookkeepingReason, Residue
from ..models import STOPPABLE_RUN_STATES, Job, RecipeRun, RunNode
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_run_plan,
    run_plan_document,
)
from ..recipe_start_payloads import validate_distributed_start_timeout_seconds
from .authority import _enqueue_recovery_stop, _recovery_authority
from .common import (
    _DAMAGED,
    _MISMATCH,
    _MISSING,
    _RECOVERY_COOLDOWN_SECONDS,
    _RECOVERY_MAX_ATTEMPTS,
    _advance_recovery_check,
    _aware,
    _encode_phases,
    _parent,
    _proves_fresh_absence,
    _RecoveryDependencyPending,
    _RecoveryJobQueue,
    _RecoveryRoutes,
    _RecoveryRunStops,
    _schedule_recovery_wait,
    _settle_unrecoverable,
    _superseded,
    _unproven,
    _unreadable_run_ids,
    release_inactive_run_claims_in_session,
    settle_observed_absent_runs_in_session,
)
from .singleton import _singleton_recovery_authority


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
                try:
                    run_plan = parse_stored_run_plan(run.plan)
                except RecipeExecutionContractError:
                    _advance_recovery_check(run, now)
                    continue
                if run_plan.execution_mode == "one-shot-jobs":
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
                        run_plan = run_plan.model_copy(
                            update={"run_generation": run.run_generation}
                        )
                        run.plan = run_plan_document(run_plan)
                        for node in run_nodes:
                            node.observed_run_generation = None
                            node.observation_process_running = None
                            node.observation_failure_diagnostics = None
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
                            run_plan = run_plan.model_copy(
                                update={"run_generation": previous_run_generation}
                            )
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
                        run_plan = run_plan.model_copy(
                            update={"run_generation": run.run_generation}
                        )
                        run.plan = run_plan_document(run_plan)
                        run_nodes[0].observed_run_generation = None
                        run_nodes[0].observation_process_running = None
                        run_nodes[0].observation_failure_diagnostics = None
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
                        deadline = authority.deadline
                        start_phases = authority.start_phases
                        workload_intent_ordinal = authority.workload_intent_ordinal
                        if (
                            not isinstance(deadline, str)
                            or not start_phases
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
                        recovery_context = DistributedRecoveryMarker(
                            schema_version=1,
                            failed_rank=0,
                            deadline=deadline,
                            start_phases=_encode_phases(start_phases),
                        )
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
                                run_plan = run_plan.model_copy(
                                    update={"run_generation": singleton_generation}
                                )
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
        self._routes.withdraw_runs(pending, pending=OperatorActionName.RETRY)
        return True

    def _settle_unreadable_runs(self, session: Session, now: datetime) -> bool:
        """Retain exact live ownership when its historical plan is unreadable.

        Plan projection failure does not prove that a workload exited. Durable
        run/node identity still permits exact observation and authorized Stop;
        claims remain until that effect is confirmed.
        """

        changed = False
        for run_id in _unreadable_run_ids(session):
            run = session.get(RecipeRun, run_id, with_for_update=True)
            if run is None or run.state not in STOPPABLE_RUN_STATES:
                continue
            note = "stored run plan is unreadable; retaining workload until exact observation or Stop"
            if run.route_error != note:
                run.route_error = note
                run.updated_at = now
                changed = True
        return changed

    @staticmethod
    def _active_recovery(session: Session, run_id: str) -> bool:
        jobs = session.scalars(
            select(Job).where(
                Job.kind.in_({"recipe.start", "recipe.stop"}),
                Job.state.in_({"queued", "running"}),
            )
        )
        for job in jobs:
            payload = _parent(job)
            if (
                not isinstance(payload, Residue)
                and payload.owner_id == run_id
                and payload.recovery is not None
            ):
                return True
        return False
