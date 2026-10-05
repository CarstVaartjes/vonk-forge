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
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import RecipeStartPayload, canonical_message
from vonk_agent_protocol.route_activation import ROUTE_EVIDENCE_MAX_AGE_SECONDS
from vonk_forge_contracts import RecipeDefinition, read_recipe

from .agent_jobs import release_owned_reservations_in_session
from .distributed_lifecycle import (
    DistributedLifecycleError,
    canonical_distributed_readiness,
)
from .lifecycle.job import JobAdapter
from .litellm import LiteLlmGeneration
from .models import (
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
    parse_stored_installation_plan,
    parse_stored_run_endpoint,
    parse_stored_run_plan,
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
from .strict_json import read_stored_model

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RECOVERY_RECHECK_SECONDS = 5
_RECOVERY_MAX_ATTEMPTS = 5
_RECOVERY_COOLDOWN_SECONDS = 300


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
    ) -> Job: ...


class _RecoveryDependencyPending(Exception):
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
                        RecipeRun.state == "running",
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
                        .where(RunNode.run_id == run.id, RunNode.state == "failed")
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
                        raise DistributedLifecycleError(
                            "accepted run has no automatic recovery authority"
                        )
                    if singleton:
                        run.run_generation += 1
                        run_plan["run_generation"] = run.run_generation
                        run.plan = run_plan_document(run_plan)
                        run_nodes[0].observed_run_generation = None
                        run_nodes[0].observation_process_running = None
                        run_nodes[0].observation_observed_at = None
                        run_nodes[0].observation_endpoint_ready = None
                    if singleton:
                        if self._recovery_run_stops is None:
                            raise DistributedLifecycleError(
                                "singleton recovery Stop owner is unavailable"
                            )
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
                            raise DistributedLifecycleError(
                                "singleton recovery authority is invalid"
                            )
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
                    else:
                        job = _enqueue_recovery_stop(
                            session,
                            self._agent_jobs,
                            run,
                            authority,
                            failed_rank=failed[0].rank,
                            now=now,
                        )
                except _RecoveryDependencyPending as pending:
                    worked = _schedule_recovery_wait(run, str(pending), now) or worked
                    continue
                except DistributedLifecycleError as error:
                    _settle_unrecoverable(run, str(error), now)
                    worked = True
                    continue
                run.route_state = "withdrawn"
                run.route_error = f"distributed recovery queued: {job.id}"
                _advance_recovery_check(run, now)
                run.updated_at = now
                queued = True
                worked = True
                break
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
                        RecipeRun.state == "running",
                        or_(
                            RecipeRun.route_next_attempt_at.is_(None),
                            RecipeRun.route_next_attempt_at <= now,
                        ),
                        select(RunNode.run_id)
                        .where(
                            RunNode.run_id == RecipeRun.id,
                            RunNode.state == "failed",
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
        raise DistributedLifecycleError("distributed recovery authority is invalid")
    enforce_recovery_deadline(payload, now=now, require_unexpired=require_unexpired)
    failed_rank = value["failed_rank"]
    deadline_value = value["deadline"]
    phases = _decode_phases(value.get("start_phases"))
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
        raise DistributedLifecycleError("distributed recovery authority is invalid")
    failed_rank = value.get("failed_rank")
    deadline_value = value.get("deadline")
    if (
        value.get("schema_version") != 1
        or type(failed_rank) is not int
        or failed_rank < 0
        or not isinstance(deadline_value, str)
    ):
        raise DistributedLifecycleError("distributed recovery authority is invalid")
    try:
        deadline = datetime.fromisoformat(deadline_value)
    except ValueError as error:
        raise DistributedLifecycleError(
            "distributed recovery authority is invalid"
        ) from error
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise DistributedLifecycleError("distributed recovery authority is invalid")
    if require_unexpired and _aware(now) >= _aware(deadline):
        raise DistributedLifecycleError("distributed recovery deadline elapsed")
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

    run.state = "failed"
    run.route_state = "withdrawn"
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
            ResourceReservation.state == "active",
            or_(RecipeRun.id.is_(None), RecipeRun.state.not_in(STOPPABLE_RUN_STATES)),
        )
        .with_for_update(of=ResourceReservation)
    ).all()
    for claim in claims:
        claim.state = "released"
        claim.released_at = now
    return bool(claims)


def settle_absent_run_in_session(
    session: Session,
    run: RecipeRun,
    run_nodes: Sequence[RunNode],
    now: datetime,
) -> None:
    """Record a run its Sparks report gone as stopped and release its claims."""

    for node in run_nodes:
        node.state = "stopped"
        node.updated_at = now
    run.state = "stopped"
    run.route_state = "withdrawn"
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
) -> dict[str, object] | None:
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
        or installation.state != "installed"
        or installation.image_digest is None
        or resolved is None
    ):
        raise DistributedLifecycleError(
            "singleton recovery installation authority is missing"
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
        raise DistributedLifecycleError("singleton recovery rank set is invalid")
    try:
        run_plan = run_plan_document(run.plan)
        installation_plan = installation_plan_document(installation.plan)
    except RecipeExecutionContractError as error:
        raise DistributedLifecycleError("singleton recovery plan is invalid") from error
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
        raise DistributedLifecycleError("singleton recovery plan authority is stale")
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
        raise DistributedLifecycleError("singleton recovery placement is invalid")
    memory_floor = plan_node.get("memory_floor_bytes")
    memory_kind = plan_node.get("memory_kind")
    if (
        type(memory_floor) is not int
        or memory_floor < 0
        or memory_kind not in {"unified", "host", "accelerator"}
    ):
        raise DistributedLifecycleError("singleton recovery resources are invalid")
    mapping = session.get(ClusterMapping, run.mapping_id)
    if (
        mapping is None
        or mapping.state != "ready"
        or mapping.generation != run.mapping_generation
        or mapping.endpoint_owner_node_id != run_node.node_id
    ):
        raise DistributedLifecycleError("singleton recovery mapping is stale")
    stop_timeout = recipe.runtime.lifecycle.stop_timeout_seconds
    start_timeout = validate_distributed_start_timeout_seconds(start_timeout_seconds)
    deadline = _aware(now) + timedelta(seconds=start_timeout + stop_timeout)
    agent_node = session.get(AgentNode, run_node.node_id)
    if agent_node is None or agent_node.state != "active":
        raise DistributedLifecycleError(
            "singleton recovery requires exact Stop and observation support"
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
            "singleton recovery waits for a fresh Controller-observed Spark presence report"
        )
    start_job, start_ordinal = _accepted_start_authority(
        session, run, revision.content_digest, run_node.node_id
    )
    accepted_start_payload = _accepted_start_authority_payload(
        session, start_job, run_node.node_id
    )
    compiled_plan = accepted_start_payload.get("compiled_execution_plan")
    try:
        accepted_start = read_stored_model(
            RecipeStartPayload,
            canonical_message(accepted_start_payload),
            from_json=True,
        )
    except (TypeError, ValueError) as error:
        raise DistributedLifecycleError("accepted Start payload is invalid") from error
    compiled_plan = accepted_start_payload.get("compiled_execution_plan")
    if not isinstance(compiled_plan, Mapping):
        raise DistributedLifecycleError("accepted Start plan is invalid")
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
            raise DistributedLifecycleError(
                f"singleton recovery {label} lifecycle differs from accepted recipe"
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
        raise DistributedLifecycleError("accepted Start image authority is stale")
    if start_job.result is not None and (
        not isinstance(start_job.result, Mapping)
        or start_job.result.get("cancel_requested") is True
    ):
        raise DistributedLifecycleError(
            "singleton recovery start authority was cancelled"
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
            compiled_execution_plan=compiled_plan,
            master_address=None,
            master_port=None,
        )
    except (KeyError, RecipeStartPayloadError) as error:
        raise DistributedLifecycleError(
            "singleton recovery start payload is invalid"
        ) from error
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
    *,
    allow_multi_target: bool = False,
    now: datetime | None = None,
) -> tuple[Job, int]:
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
        raise DistributedLifecycleError(
            "singleton recovery lacks exact accepted Start authority"
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
            raise DistributedLifecycleError(
                "singleton recovery lacks current-generation Start authority"
            )
        if allow_multi_target:
            if now is None:
                raise DistributedLifecycleError(
                    "multi-node recovery observation time is unavailable"
                )
            _validate_distributed_recovery_start_origin(
                session,
                start,
                run=run,
                targets=targets,
                workload_intent_ordinal=ordinal,
                now=now,
            )
        else:
            _validate_singleton_recovery_start_origin(
                session,
                start,
                run=run,
                recipe_digest=recipe_digest,
                targets=targets,
                workload_intent_ordinal=ordinal,
            )
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
        raise DistributedLifecycleError(
            "singleton recovery lacks exact current Start authority"
        )
    _accepted_start_authority_payload(
        session,
        start,
        node_id,
        run=run if allow_multi_target else None,
        recipe_digest=recipe_digest if allow_multi_target else None,
        targets=targets if allow_multi_target else None,
        expected_generation=run.run_generation if allow_multi_target else None,
        allow_multi_target=allow_multi_target,
    )
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
) -> None:
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
        raise DistributedLifecycleError("singleton recovery Start marker is invalid")
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
        raise DistributedLifecycleError(
            "singleton recovery Start lacks its exact completed Stop"
        )
    recovery = stop.payload.get("recovery")
    if stop.state != "succeeded" or stop.actor != "system:singleton-recovery":
        raise DistributedLifecycleError(
            "singleton recovery Stop did not complete under its recovery actor"
        )
    if (
        stop.authority_revision != run.plan_digest
        or stop.targets != targets
        or stop.payload.get("workload_intent_ordinal") != workload_intent_ordinal
        or stop.payload_digest
        != hashlib.sha256(canonical_message(stop.payload)).hexdigest()
    ):
        raise DistributedLifecycleError(
            "singleton recovery Stop has stale exact run authority"
        )
    if (
        not isinstance(recovery, Mapping)
        or set(recovery)
        != {"schema_version", "failed_rank", "deadline", "start_phases"}
        or any(recovery.get(key) != marker.get(key) for key in marker)
    ):
        raise DistributedLifecycleError(
            "singleton recovery Stop continuation differs from its Start"
        )
    phases = _decode_phases(recovery.get("start_phases"))
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
        raise DistributedLifecycleError(
            "singleton recovery Start differs from its exact Stop continuation"
        )


def _validate_distributed_recovery_start_origin(
    session: Session,
    start: Job,
    *,
    run: RecipeRun,
    targets: Sequence[str],
    workload_intent_ordinal: int,
    now: datetime,
) -> None:
    marker = start.payload.get("recovery")
    if (
        not isinstance(marker, Mapping)
        or set(marker) != {"schema_version", "failed_rank", "deadline"}
        or marker.get("schema_version") != 1
        or type(marker.get("failed_rank")) is not int
        or not 0 <= marker["failed_rank"] < len(targets)
        or not isinstance(marker.get("deadline"), str)
        or start.payload.get("workload_intent_ordinal") != workload_intent_ordinal
    ):
        raise DistributedLifecycleError("distributed recovery Start marker is invalid")
    stop_request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:distributed-recovery:{run.id}:{marker['failed_rank']}:{marker['deadline']}",
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
    if (
        stop is None
        or stop.state != "succeeded"
        or stop.actor != "system:distributed-recovery"
        or start.actor != "system:distributed-recovery"
        or stop.authority_revision != run.plan_digest.removeprefix("sha256:")
        or stop.targets != list(targets)
        or stop.payload.get("plan_digest") != run.plan_digest
        or stop.payload.get("workload_intent_ordinal") != workload_intent_ordinal
        or stop.payload_digest
        != hashlib.sha256(canonical_message(stop.payload)).hexdigest()
        or (
            isinstance(stop.result, Mapping)
            and stop.result.get("cancel_requested") is True
        )
    ):
        raise DistributedLifecycleError("distributed recovery Stop is stale")
    try:
        continuation = recovery_start_plan(
            stop.payload, now=now, require_unexpired=False
        )
    except DistributedLifecycleError as error:
        raise DistributedLifecycleError(
            "distributed recovery Stop continuation is invalid"
        ) from error
    if continuation is None:
        raise DistributedLifecycleError(
            "distributed recovery Stop continuation is missing"
        )
    start_phases, stop_marker = continuation
    projected_start_phases = _project_start_phases(start.payload.get("phases"))
    if (
        canonical_message(dict(stop_marker)) != canonical_message(dict(marker))
        or start.payload.get("start_deadline") != marker.get("deadline")
        or projected_start_phases is None
        or canonical_message(_encode_phases(start_phases))
        != canonical_message(projected_start_phases)
        or start.request_id
        != str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"vonk:distributed-recovery-start:{stop.id}",
            )
        )
    ):
        raise DistributedLifecycleError(
            "distributed recovery Start differs from its exact Stop continuation"
        )


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
    session: Session,
    start: Job,
    node_id: str,
    *,
    run: RecipeRun | None = None,
    recipe_digest: str | None = None,
    targets: Sequence[str] | None = None,
    expected_generation: int | None = None,
    allow_multi_target: bool = False,
) -> Mapping[str, object]:
    phases = start.payload.get("phases")
    if not isinstance(phases, list) or not phases:
        raise DistributedLifecycleError("accepted Start payload is missing")
    if not allow_multi_target:
        if sum(len(phase) for phase in phases if isinstance(phase, list)) != 1:
            raise DistributedLifecycleError("accepted Start payload is not singleton")
        items = [
            item
            for phase in phases
            if isinstance(phase, list)
            for item in phase
            if isinstance(item, Mapping) and item.get("node_id") == node_id
        ]
        if len(items) != 1:
            raise DistributedLifecycleError("accepted Start payload is not singleton")
        return dict(_accepted_start_child(session, start, node_id, items[0]).payload)

    if (
        run is None
        or recipe_digest is None
        or expected_generation is None
        or not targets
        or len(targets) < 2
        or node_id not in targets
        or start.targets != list(targets)
    ):
        raise DistributedLifecycleError("accepted Start target set is invalid")
    items: list[Mapping[str, object]] = []
    phase_targets: set[str] = set()
    operation_ids: set[str] = set()
    for phase in phases:
        if not isinstance(phase, list) or not phase:
            raise DistributedLifecycleError("accepted Start phase is invalid")
        for item in phase:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"operation_id", "node_id", "payload"}
                or not isinstance(item.get("operation_id"), str)
                or not isinstance(item.get("node_id"), str)
                or not isinstance(item.get("payload"), Mapping)
            ):
                raise DistributedLifecycleError("accepted Start phase item is invalid")
            try:
                uuid.UUID(item["operation_id"])
            except ValueError as error:
                raise DistributedLifecycleError(
                    "accepted Start operation identity is invalid"
                ) from error
            if item["node_id"] not in targets or item["operation_id"] in operation_ids:
                raise DistributedLifecycleError("accepted Start target set is invalid")
            phase_targets.add(item["node_id"])
            operation_ids.add(item["operation_id"])
            if item["node_id"] == node_id:
                items.append(item)
    if phase_targets != set(targets) or not items:
        raise DistributedLifecycleError("accepted Start target set is incomplete")

    deadline = start.payload.get("start_deadline")
    if deadline is not None and not isinstance(deadline, str):
        raise DistributedLifecycleError("accepted Start deadline is invalid")
    launch_phase = "rank-launch" if deadline is not None else None
    bound_items: list[tuple[AgentOperation, RecipeStartPayload]] = []
    for item in items:
        child = _accepted_start_child(session, start, node_id, item)
        try:
            typed = read_stored_model(
                RecipeStartPayload, canonical_message(child.payload), from_json=True
            )
        except (TypeError, ValueError) as error:
            raise DistributedLifecycleError(
                "accepted Start child is invalid"
            ) from error
        bound_items.append((child, typed))
    launches = [item for item in bound_items if item[1].phase == launch_phase]
    readiness = [
        item for item in bound_items if item[1].phase == "collective-readiness"
    ]
    if len(launches) != 1 or len(launches) + len(readiness) != len(bound_items):
        raise DistributedLifecycleError("accepted Start rank phase is invalid")
    endpoint_owner = _validate_multi_start_payload(
        session,
        run,
        start,
        node_id,
        recipe_digest,
        targets,
        expected_generation,
        launches[0][1],
    )
    if len(readiness) != int(endpoint_owner and deadline is not None):
        raise DistributedLifecycleError("accepted Start readiness phase is invalid")
    if readiness:
        launch_document = launches[0][1].model_dump(mode="json")
        readiness_document = readiness[0][1].model_dump(mode="json")
        launch_document.pop("phase", None)
        readiness_document.pop("phase", None)
        if canonical_message(launch_document) != canonical_message(readiness_document):
            raise DistributedLifecycleError(
                "accepted Start readiness differs from its rank launch"
            )
    return dict(launches[0][0].payload)


def _accepted_start_child(
    session: Session,
    start: Job,
    node_id: str,
    item: Mapping[str, object],
) -> AgentOperation:
    operation_id = item.get("operation_id")
    payload = item.get("payload")
    if not isinstance(operation_id, str) or not isinstance(payload, Mapping):
        raise DistributedLifecycleError("accepted Start child identity is invalid")
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
        raise DistributedLifecycleError("accepted Start payload binding is invalid")
    return child


def _validate_multi_start_payload(
    session: Session,
    run: RecipeRun,
    start: Job,
    node_id: str,
    recipe_digest: str,
    targets: Sequence[str],
    expected_generation: int,
    typed: RecipeStartPayload,
) -> bool:
    installation = session.get(RecipeInstallation, run.installation_id)
    run_node = session.scalar(
        select(RunNode).where(RunNode.run_id == run.id, RunNode.node_id == node_id)
    )
    try:
        stored_run = parse_stored_run_plan(run.plan)
        stored_installation = (
            parse_stored_installation_plan(installation.plan)
            if installation is not None
            else None
        )
    except RecipeExecutionContractError as error:
        raise DistributedLifecycleError("accepted Start plan is invalid") from error
    planned_node = next(
        (node for node in stored_run.nodes if node.node_id == node_id), None
    )
    owners = [node for node in stored_run.nodes if node.endpoint_owner]
    if (
        installation is None
        or run_node is None
        or stored_installation is None
        or planned_node is None
        or len(owners) != 1
        or tuple(sorted(node.node_id for node in stored_run.nodes)) != tuple(targets)
        or tuple(sorted(stored_installation.compiled_execution_plans)) != tuple(targets)
        or run.installation_id != installation.id
        or run.mapping_id != installation.mapping_id
        or run.mapping_generation != installation.mapping_generation
        or stored_run.installation_id != run.installation_id
        or stored_run.mapping_id != run.mapping_id
        or stored_run.mapping_generation != run.mapping_generation
        or stored_run.recipe_revision_id != installation.recipe_revision_id
        or stored_run.plan_digest != run.plan_digest
        or stored_run.run_generation != run.run_generation
        or stored_run.alias != run.alias
        or stored_installation.mapping_id != installation.mapping_id
        or stored_installation.mapping_generation != installation.mapping_generation
        or stored_installation.recipe_revision_id != installation.recipe_revision_id
        or stored_installation.plan_digest != installation.plan_digest
        or stored_installation.image_digest != installation.image_digest
        or stored_installation.recipe_content_sha256 != recipe_digest
        or expected_generation != run.run_generation
    ):
        raise DistributedLifecycleError("accepted Start plan identity is stale")

    endpoint_owner = planned_node.endpoint_owner
    if endpoint_owner:
        try:
            endpoint = parse_stored_run_endpoint(run_node.endpoint)
        except RecipeExecutionContractError as error:
            raise DistributedLifecycleError(
                "accepted Start owner endpoint is invalid"
            ) from error
        if endpoint is None:
            raise DistributedLifecycleError("accepted Start owner endpoint is missing")
        try:
            parsed_endpoint = urlsplit(endpoint.url)
            endpoint_address = parsed_endpoint.hostname
            endpoint_port = parsed_endpoint.port
        except ValueError as error:
            raise DistributedLifecycleError(
                "accepted Start owner endpoint is invalid"
            ) from error
        if (
            parsed_endpoint.scheme != "http"
            or parsed_endpoint.username is not None
            or parsed_endpoint.password is not None
            or parsed_endpoint.path not in {"", "/"}
            or parsed_endpoint.query
            or parsed_endpoint.fragment
            or endpoint_address is None
            or endpoint_port != run_node.port
        ):
            raise DistributedLifecycleError("accepted Start owner endpoint is invalid")
    else:
        endpoint_address = planned_node.fabric_address
        if not isinstance(endpoint_address, str):
            raise DistributedLifecycleError("accepted Start fabric address is missing")
    master_address = owners[0].fabric_address if len(targets) > 1 else None
    master_port = owners[0].rendezvous_port if len(targets) > 1 else None
    if len(targets) > 1 and (
        not isinstance(master_address, str) or master_port is None
    ):
        raise DistributedLifecycleError("accepted Start rendezvous plan is invalid")
    deadline = start.payload.get("start_deadline")
    if deadline is not None and not isinstance(deadline, str):
        raise DistributedLifecycleError("accepted Start deadline is invalid")
    compiled = stored_installation.compiled_execution_plans[node_id]
    try:
        expected = build_recipe_start_payload(
            run_id=run.id,
            installation_id=installation.id,
            recipe_revision_id=installation.recipe_revision_id,
            mapping_id=run.mapping_id,
            run_generation=expected_generation,
            plan_digest=run.plan_digest,
            placement=RecipeStartPlacement(
                node_id,
                planned_node.rank,
                planned_node.role,
                planned_node.port,
                planned_node.required_memory_bytes,
                planned_node.memory_floor_bytes,
                planned_node.memory_kind,
                planned_node.fabric_address,
            ),
            compiled_endpoint_address=endpoint_address if endpoint_owner else None,
            world_size=len(targets),
            compiled_execution_plan=compiled.model_dump(mode="json"),
            master_address=master_address,
            master_port=master_port,
            phase="rank-launch" if deadline is not None else None,
            start_deadline=deadline,
        )
        expected_typed = read_stored_model(
            RecipeStartPayload, canonical_message(expected), from_json=True
        )
    except (RecipeStartPayloadError, TypeError, ValueError) as error:
        raise DistributedLifecycleError(
            "accepted Start plan cannot be rebound"
        ) from error
    if canonical_message(typed) != canonical_message(expected_typed):
        raise DistributedLifecycleError(
            "accepted Start child differs from stored execution plan"
        )
    return endpoint_owner


def _recovery_authority(
    session: Session,
    run: RecipeRun,
    now: datetime,
    failed_rank: int,
    *,
    stop_run_generation: int,
) -> dict[str, object] | None:
    installation = session.get(RecipeInstallation, run.installation_id)
    resolved = (
        _active_recipe_revision(session, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    if installation is None or resolved is None or installation.image_digest is None:
        raise DistributedLifecycleError("distributed recovery authority is missing")
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
        raise DistributedLifecycleError("distributed recovery rank set is invalid")
    try:
        run_plan = run_plan_document(run.plan)
        installation_plan = installation_plan_document(installation.plan)
    except RecipeExecutionContractError as error:
        raise DistributedLifecycleError(
            "distributed recovery plan is invalid"
        ) from error
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
        raise DistributedLifecycleError("distributed recovery run authority is stale")
    plans = run_plan.get("nodes")
    compiled_plans = installation_plan.get("compiled_execution_plans")
    if (
        not isinstance(plans, list)
        or len(plans) != len(nodes)
        or not isinstance(compiled_plans, Mapping)
    ):
        raise DistributedLifecycleError("distributed recovery plan is invalid")
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
        raise DistributedLifecycleError("distributed recovery plan is invalid")
    owner = owners[0]
    master_address = owner.get("fabric_address")
    master_port = owner.get("rendezvous_port")
    if not isinstance(master_address, str) or type(master_port) is not int:
        raise DistributedLifecycleError("distributed recovery rendezvous is invalid")
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
                "Spark presence report"
            )
        presences[node.node_id] = presence.management_address
    start_job, startup_budget = _original_start_authority(
        session, run, revision.content_digest
    )
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
            raise DistributedLifecycleError("distributed recovery plan is invalid")
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
                compiled_execution_plan=compiled_plan,
                master_address=master_address,
                master_port=master_port,
                phase="rank-launch",
                start_deadline=start_deadline,
            )
        except (KeyError, RecipeStartPayloadError) as error:
            raise DistributedLifecycleError(
                "distributed recovery start payload is invalid"
            ) from error
        start_payloads[node.role] = (node.node_id, payload)
    start_order = topology.start_order
    roles = {node.role for node in nodes}
    if (
        set(start_order) != roles
        or set(stop_order) != roles
        or len(start_order) != len(roles)
        or len(stop_order) != len(roles)
    ):
        raise DistributedLifecycleError("distributed recovery order is invalid")
    owner_role = owner.get("role")
    if not isinstance(owner_role, str) or owner_role not in start_payloads:
        raise DistributedLifecycleError("distributed recovery endpoint is invalid")
    owner_node_id, owner_payload = start_payloads[owner_role]
    if type(stop_run_generation) is not int or stop_run_generation < 1:
        raise DistributedLifecycleError(
            "distributed recovery prior Start generation is invalid"
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
        raise DistributedLifecycleError(
            "distributed recovery lacks exact prior Start Stop authority"
        ) from error
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
) -> Job:
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
        raise DistributedLifecycleError("distributed recovery authority is invalid")
    stop_phases = tuple(tuple(group) for group in raw_stop_phases)
    start_phases = tuple(tuple(group) for group in raw_start_phases)
    request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:distributed-recovery:{run.id}:{failed_rank}:{deadline}",
        )
    )
    if session.scalar(select(Job.id).where(Job.request_id == request_id)):
        raise DistributedLifecycleError("distributed recovery is already queued")
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
        raise DistributedLifecycleError(
            "distributed recovery start authority was superseded"
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
) -> tuple[Job, timedelta]:
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
        raise DistributedLifecycleError(
            "distributed recovery lacks its start authority"
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
        raise DistributedLifecycleError(
            "distributed recovery start authority is invalid"
        )
    try:
        deadline = _aware(datetime.fromisoformat(deadline_value))
        # PostgreSQL returns an aware UTC value; SQLite's test adapter drops
        # its timezone from this database-owned timestamp.
        created = start.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        duration = deadline - _aware(created)
        seconds = duration.total_seconds()
        if not seconds.is_integer():
            raise ValueError("startup budget must be whole seconds")
        validate_distributed_start_timeout_seconds(int(seconds))
    except (ValueError, DistributedLifecycleError) as error:
        raise DistributedLifecycleError(
            "distributed recovery start authority is invalid"
        ) from error
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
) -> tuple[tuple[tuple[str, Mapping[str, object]], ...], ...]:
    if not isinstance(value, list) or not value:
        raise DistributedLifecycleError("distributed recovery phases are invalid")
    phases: list[tuple[tuple[str, Mapping[str, object]], ...]] = []
    for raw_group in value:
        if not isinstance(raw_group, list) or not raw_group:
            raise DistributedLifecycleError("distributed recovery phases are invalid")
        group: list[tuple[str, Mapping[str, object]]] = []
        for item in raw_group:
            if not isinstance(item, Mapping) or set(item) != {"node_id", "payload"}:
                raise DistributedLifecycleError(
                    "distributed recovery phases are invalid"
                )
            node_id = item.get("node_id")
            item_payload = item.get("payload")
            if not isinstance(node_id, str) or not isinstance(item_payload, Mapping):
                raise DistributedLifecycleError(
                    "distributed recovery phases are invalid"
                )
            group.append((node_id, dict(item_payload)))
        phases.append(tuple(group))
    return tuple(phases)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DistributedLifecycleError("distributed recovery clock is invalid")
    return value.astimezone(UTC)


__all__ = [
    "DistributedRecoveryCoordinator",
    "enforce_recovery_deadline",
    "recovery_start_plan",
]
