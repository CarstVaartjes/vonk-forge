"""Intent for digest-bound recipe operations."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
)

from .. import job_states
from ..lifecycle.evidence import (
    BookkeepingReason,
    Damaged,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from ..models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    InstallationNode,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..recipe_progress import (
    _cancel_requested as _cancel_requested,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _parent_intent as _parent_intent,  # noqa: PLC0414 -- shared helper export
)
from .errors import RecipeRequestInvalid, RecipeStopAuthorityRefused
from .rank_authority import _run_is_one_shot


def _bound_workload_intent(job: Job) -> int:
    """The exact intent a job was admitted under; a fence, so it never defaults."""

    ordinal = _parent_intent(job)
    if type(ordinal) is not int or ordinal < 1:
        raise RecipeStopAuthorityRefused("workload operation lacks its admitted intent")
    return ordinal


def _job_workload_intent(session: Session, job: Job) -> int | None:
    """The intent a job was admitted under, re-derived from its orders when its
    own payload lost it; ``None`` when nothing proves it (the caller then treats
    the job as no longer current, never as the newest intent)."""

    def read() -> int | Damaged:
        ordinal = _parent_intent(job)
        if type(ordinal) is not int or ordinal < 1:
            return Damaged("job payload carries no workload intent")
        return ordinal

    def rebuild() -> int | None:
        ordinals = {
            child.workload_intent_ordinal
            for child in session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
            )
        }
        return ordinals.pop() if len(ordinals) == 1 else None

    loaded = read_or_rebuild(
        kind="recipe.workload-intent", subject=job.id, read=read, rebuild=rebuild
    )
    if isinstance(loaded, Residue) or loaded < 1:
        return None
    return loaded


def _run_start_intent(session: Session, run_id: str) -> int:
    run = session.get(RecipeRun, run_id)
    if run is None:
        raise RecipeRequestInvalid("recipe run does not exist")
    root_kind = (
        "recipe.job.activate.v1"
        if _run_is_one_shot(session, run)
        else WireAgentOperation.RECIPE_START.value
    )
    starts = tuple(
        session.scalars(
            select(Job)
            .where(
                Job.kind == root_kind,
                Job.payload["owner_kind"].as_string() == "run",
                Job.payload["owner_id"].as_string() == run_id,
                Job.payload["recovery"].as_string().is_(None),
            )
            .order_by(Job.created_at, Job.id)
            .limit(2)
        )
    )
    if len(starts) != 1:
        raise RecipeStopAuthorityRefused(
            "recipe run lacks its original workload authority"
        )
    return _bound_workload_intent(starts[0])


def _intent_is_current(session: Session, ordinal: int, targets: Sequence[str]) -> bool:
    nodes = tuple(
        session.scalars(
            select(AgentNode)
            .where(AgentNode.node_id.in_(targets))
            .order_by(AgentNode.node_id)
            .with_for_update(of=AgentNode)
        )
    )
    return tuple(node.node_id for node in nodes) == tuple(sorted(set(targets))) and all(
        node.workload_intent_ordinal == ordinal for node in nodes
    )


def _shared_workload_intent(session: Session, targets: Sequence[str]) -> int | None:
    """The one workload intent every target Spark currently shares, if there is one.

    A cleanup of what an older, superseded order may have launched is admitted as
    a child of the intent that owns the Sparks now: it takes no new intent and so
    never cancels the newer work, and the stale ordinal of the failed order (which
    would be refused) is not needed.
    """

    nodes = tuple(
        session.scalars(
            select(AgentNode)
            .where(AgentNode.node_id.in_(targets))
            .order_by(AgentNode.node_id)
            .with_for_update(of=AgentNode)
        )
    )
    ordinals = {node.workload_intent_ordinal for node in nodes}
    if len(nodes) != len(set(targets)) or len(ordinals) != 1:
        return None
    ordinal = ordinals.pop()
    return ordinal if ordinal >= 1 else None


def _workload_owner_scope(
    session: Session, kind: str, owner_id: str
) -> tuple[str, ...]:
    """The Sparks an owner's rows place work on; empty when its rows are damaged.

    An owner row that names no Spark (or one twice) is bookkeeping damage: it is
    retired as unknown and callers use the Sparks each job itself names.
    """

    if kind in {
        WireAgentOperation.RECIPE_START.value,
        WireAgentOperation.RECIPE_STOP.value,
    }:
        if session.get(RecipeRun, owner_id) is None:
            raise RecipeRequestInvalid("recipe run does not exist")
        statement = select(RunNode.node_id).where(RunNode.run_id == owner_id)
    elif kind in {
        WireAgentOperation.RECIPE_INSTALL.value,
        WireAgentOperation.RECIPE_UNINSTALL.value,
        WireAgentOperation.RECIPE_RECONCILE.value,
    }:
        if session.get(RecipeInstallation, owner_id) is None:
            raise RecipeRequestInvalid("recipe installation does not exist")
        statement = select(InstallationNode.node_id).where(
            InstallationNode.installation_id == owner_id
        )
    else:
        raise RecipeRequestInvalid("workload reconciliation kind is invalid")
    targets = tuple(sorted(session.scalars(statement)))
    if not targets or len(targets) != len(set(targets)):
        retire_as_unknown(
            "recipe.workload-owner-scope",
            owner_id,
            BookkeepingReason.ROW_INCOMPLETE,
            f"{kind} owner has no exact Spark membership",
        )
        return ()
    return targets


def _profile_effect_scope(
    owner_scope: tuple[str, ...], profile_target_node_ids: Sequence[str] | None
) -> tuple[str, ...]:
    """Validate FleetProfile's reachable effect subset against the full owner."""
    if profile_target_node_ids is None:
        return owner_scope
    targets = tuple(profile_target_node_ids)
    if (
        not targets
        or targets != tuple(sorted(set(targets)))
        # An owner whose rows name no Spark (retired as unknown) proves no subset.
        or (owner_scope and not set(targets) < set(owner_scope))
    ):
        raise RecipeRequestInvalid("profile Stop target scope is invalid")
    return targets


def _active_owned_workload_jobs(
    session: Session,
    kind: str,
    owner_id: str,
    *,
    lock: bool = False,
    include_waiting_cancellation: bool = False,
) -> tuple[Job, ...]:
    owner_kind = (
        "run"
        if kind
        in {WireAgentOperation.RECIPE_START.value, WireAgentOperation.RECIPE_STOP.value}
        else "installation"
    )
    statement = (
        select(Job)
        .where(
            Job.kind == kind,
            Job.state.in_(
                job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.NEEDS_OPERATOR,
                )
                if include_waiting_cancellation
                else (LifecycleState.QUEUED.value, LifecycleState.RUNNING.value)
            ),
            Job.payload["owner_kind"].as_string() == owner_kind,
            Job.payload["owner_id"].as_string() == owner_id,
        )
        .order_by(Job.id)
    )
    if lock:
        statement = statement.with_for_update(of=Job)
    return tuple(
        job
        for job in session.scalars(statement)
        if job.state not in job_states.words(LifecycleState.NEEDS_OPERATOR)
        or (_cancel_requested(job))
    )


def _unissued_workload_children(
    session: Session, job: Job, *, lock: bool = False
) -> tuple[AgentOperation, ...] | None:
    ordinal = _parent_intent(job)
    if type(ordinal) is not int or ordinal < 1:
        return None
    statement = (
        select(AgentOperation)
        .where(AgentOperation.parent_job_id == job.id)
        .order_by(AgentOperation.id)
    )
    if lock:
        statement = statement.with_for_update(of=AgentOperation)
    children = tuple(session.scalars(statement))
    if not children or any(
        child.state != LifecycleState.QUEUED.value
        or child.current_attempt != 0
        or child.workload_intent_ordinal != ordinal
        or child.node_id not in job.targets
        for child in children
    ):
        return None
    if (
        session.scalar(
            select(AgentOperationAttempt.id)
            .where(
                AgentOperationAttempt.operation_id.in_(child.id for child in children)
            )
            .limit(1)
        )
        is not None
    ):
        return None
    return children


def _cancel_reason(value: object) -> str:
    reason = " ".join(str(value).split())
    if not reason:
        raise RecipeRequestInvalid(
            "cancellation reason is required", reason=InvalidRequestReason.INCOMPLETE
        )
    return reason[:512]
