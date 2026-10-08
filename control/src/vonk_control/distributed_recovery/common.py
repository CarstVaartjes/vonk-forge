"""Distributed recovery: common concerns."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InvalidRequestReason,
    RecipeStartPayload,
    RecipeStopPayload,
    ReservationState,
    RouteState,
    RunState,
    canonical_message,
)
from vonk_agent_protocol.route_activation import ROUTE_EVIDENCE_MAX_AGE_SECONDS
from vonk_forge_contracts import RecipeDefinition, read_recipe

from ..agent_jobs import release_owned_reservations_in_session
from ..categorized_errors import UnsettledOutcome
from ..distributed_lifecycle import DistributedRecoveryInvalid
from ..job_documents import (
    DistributedRecoveryMarker,
    RecipeStartParent,
    RecipeStopParent,
    RecoveryStartItem,
)
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..litellm import LiteLlmGeneration
from ..models import (
    STOPPABLE_NOT_RUNNING_RUN_STATES,
    STOPPABLE_RUN_STATES,
    AgentNode,
    AgentOperation,
    CatalogDocumentRevision,
    Job,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_run_plan,
)
from ..recipe_lifecycle_contract import RecipeOperationCancellationResult
from ..reservation_owners import run_has_live_operation
from ..stored_json import read_row_column

"""Durable controller coordination for distributed recipe recovery."""


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


_RECOVERY_RECHECK_SECONDS = 5


_RECOVERY_MAX_ATTEMPTS = 5


_RECOVERY_COOLDOWN_SECONDS = 300


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


def _parent(job: Job) -> RecipeStartParent | RecipeStopParent | Residue:
    value = read_row_column(job, "payload")
    if isinstance(value, RecipeStartParent | RecipeStopParent | Residue):
        return value
    return _unproven(job.id, _DAMAGED, "stored recipe parent is invalid")


RecoveryStartPhases = tuple[tuple[tuple[str, RecipeStartPayload], ...], ...]


RecoveryStopPhases = tuple[tuple[tuple[str, RecipeStopPayload], ...], ...]


@dataclass(frozen=True, slots=True)
class _RecoveryAuthority:
    deadline: str
    workload_intent_ordinal: int | None
    failed_rank: int
    recipe_content_sha256: str
    start_phases: RecoveryStartPhases
    stop_phases: RecoveryStopPhases = ()


def _cancelled(job: Job) -> bool:
    result = read_row_column(job, "result")
    return isinstance(result, Residue) or (
        isinstance(result, RecipeOperationCancellationResult)
        and result.cancel_requested
    )


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
        recovery_context: DistributedRecoveryMarker,
        workload_intent_ordinal: int,
        now: datetime,
    ) -> Job | Residue: ...


class _RecoveryDependencyPending(UnsettledOutcome):
    """A recoverable precondition is not ready yet; keep the run current."""


def _unreadable_run_ids(session: Session) -> list[str]:
    unreadable = []
    for run in session.scalars(
        select(RecipeRun).where(RecipeRun.state.in_(STOPPABLE_RUN_STATES))
    ):
        try:
            parse_stored_run_plan(run.plan)
        except RecipeExecutionContractError:
            unreadable.append(run.id)
    return unreadable


def _recovery_marker(payload: object) -> DistributedRecoveryMarker | None:
    if isinstance(payload, DistributedRecoveryMarker):
        return payload
    if isinstance(payload, RecipeStartParent | RecipeStopParent):
        return payload.recovery
    # The exported boundary also accepts a retained raw Job document.
    value = payload.get("recovery") if isinstance(payload, Mapping) else None
    if value is None:
        return None
    try:
        return DistributedRecoveryMarker.model_validate_json(canonical_message(value))
    except (TypeError, ValueError) as error:
        raise DistributedRecoveryInvalid(
            "distributed recovery authority is invalid",
            reason=InvalidRequestReason.MALFORMED,
        ) from error


def recovery_start_plan(
    payload: object,
    *,
    now: datetime,
    require_unexpired: bool = True,
) -> tuple[RecoveryStartPhases, DistributedRecoveryMarker] | None:
    """Decode the canonical start phases carried by a recovery stop job."""
    marker = _recovery_marker(payload)
    if marker is None:
        return None
    enforce_recovery_deadline(marker, now=now, require_unexpired=require_unexpired)
    phases = _decode_phases(marker.start_phases)
    if phases is None:
        raise DistributedRecoveryInvalid(
            "distributed recovery phases are invalid",
            reason=InvalidRequestReason.MALFORMED,
        )
    return phases, marker.model_copy(update={"start_phases": None})


def enforce_recovery_deadline(
    payload: object,
    *,
    now: datetime,
    require_unexpired: bool = True,
) -> bool:
    """Validate and enforce a retained recovery marker at a trust boundary."""
    marker = _recovery_marker(payload)
    if marker is None:
        return False
    deadline = datetime.fromisoformat(marker.deadline)
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
        run_plan = parse_stored_run_plan(run.plan)
    except RecipeExecutionContractError:
        return False
    run_nodes = run_plan.nodes
    if (
        run_plan.observation_schema_version != 2
        or run_plan.run_generation != run.run_generation
        or run_plan.execution_mode == "one-shot-jobs"
        or len(run_nodes) != 1
        or run_nodes[0].node_id != node.node_id
        or run_nodes[0].rank != 0
        or run_nodes[0].role != "entrypoint"
        or run_nodes[0].endpoint_owner is not True
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


def _encode_phases(phases: RecoveryStartPhases) -> list[list[RecoveryStartItem]]:
    return [
        [
            RecoveryStartItem(node_id=node_id, payload=payload)
            for node_id, payload in group
        ]
        for group in phases
    ]


def _decode_phases(
    value: list[list[RecoveryStartItem]] | None,
) -> RecoveryStartPhases | None:
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(group, list)
            or not group
            or any(not isinstance(item, RecoveryStartItem) for item in group)
            for group in value
        )
    ):
        return None
    return tuple(
        tuple((item.node_id, item.payload) for item in group) for group in value
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DistributedRecoveryInvalid(
            "distributed recovery clock is invalid",
            reason=InvalidRequestReason.MALFORMED,
        )
    return value.astimezone(UTC)


__all__ = ["_RecoveryJobQueue", "_RecoveryRoutes", "_RecoveryRunStops"]
