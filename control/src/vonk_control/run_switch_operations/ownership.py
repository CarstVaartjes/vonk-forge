"""Ownership."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from vonk_agent_protocol import (
    LifecycleState,
    RunSwitchCode,
    WaitReason,
)

from .. import job_states
from ..models import (
    AgentNode,
    Job,
)
from ..recipe_builds import RecipeBuildAdmissionBusy
from ..run_switch_contract import (
    RunSwitchOperationResult,
    RunSwitchPlan,
)
from ..stored_json import read_row_column
from .errors import RunSwitchRetryLater, _RunSwitchBuildParentChanged
from .observation_helpers import _phase_request_key
from .planning_helpers import _run_switch_payload, _stored_job_plan
from .provider import _ADAPTER
from .result_helpers import _bound_workload_intent, _persisted_result, _read_progress


def _lock_phase_owner(
    session: Session, request_key: str, phase_index: int, item_index: int
) -> Job | None:
    """Lock the RunSwitch whose phase ``request_key`` names, or return None.

    A phase runs under its operation's own request key or, after a retry or
    re-plan, under the current generation's ``_phase_request_key``. Both name
    the same owner; a key from an earlier generation names none. Only the
    matched row is locked (``NOWAIT``; the caller maps a busy lock and checks
    the owner's state and checkpoint itself).
    """

    def locked(condition: ColumnElement[bool]) -> Job | None:
        return session.scalar(
            select(Job)
            .where(condition)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )

    def current_key(job: Job) -> bool:
        generation = _read_progress(
            read_row_column(job, "result")
        ).phase_retry_generation
        return (
            type(generation) is int
            and generation > 0
            and request_key
            == _phase_request_key(job.request_id, phase_index, item_index, generation)
        )

    job = locked(Job.request_id == request_key)
    if job is not None:
        return job
    # A derived key cannot be inverted; only an active operation can own it.
    owner = next(
        (
            job
            for job in session.scalars(
                select(Job).where(
                    Job.kind == "recipe.run-switch.v2",
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.OBSERVING,
                        )
                    ),
                )
            )
            if current_key(job)
        ),
        None,
    )
    job = locked(Job.id == owner.id) if owner is not None else None
    return job if job is not None and current_key(job) else None


def _complete_cancellation(
    job: Job, progress: RunSwitchOperationResult, now: datetime
) -> None:
    """End a cancelled operation: the child's phase ended (or none was issued).

    A cancellation record that cannot be read does not refuse the cancel: the
    operation still ends ``cancelled`` (rule 4), just without the requester's text.
    """

    _ADAPTER.cancelled(job, progress, now, reason=None)
    job.result = _persisted_result(progress)
    job.updated_at = now


def _lock_current_build_parent(
    session: Session,
    *,
    plan: RunSwitchPlan,
    phase_index: int,
    item_index: int,
    actor: str,
    request_key: str,
    ordinal: int,
    child_id: object = None,
    cancellation: object = None,
) -> Job:
    """Fence build admission/observations with the same current parent intent.

    Lock scope nodes (including an external builder) in stable order, then the
    parent, before the lifecycle service locks the build. All acquisition is
    nonblocking and the caller retains these locks through its SQL commit.
    """
    from .retry_holds import RetryHoldsMixin

    if not 0 <= phase_index < len(plan.phases) or (
        plan.phases[phase_index].kind,
        plan.phases[phase_index].subphase,
        plan.phases[phase_index].state,
    ) != ("prepare", "container-build", "planned"):
        raise RunSwitchRetryLater(
            RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID, reason=WaitReason.STALE_PLAN
        )
    targets = {node.node_id for node in plan.spark_group.nodes}
    locked_nodes = targets | (
        {plan.build.builder_node_id} if plan.build.builder_node_id else set()
    )
    try:
        nodes = tuple(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(locked_nodes))
                .order_by(AgentNode.node_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        )
        job = _lock_phase_owner(session, request_key, phase_index, item_index)
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) != "55P03":
            raise
        raise RecipeBuildAdmissionBusy() from error
    if (
        job is None
        or job.kind != "recipe.run-switch.v2"
        or job.actor != actor
        or set(job.targets) != targets
        or len(nodes) != len(locked_nodes)
        or (payload := _run_switch_payload(job)) is None
        or payload.workload_intent_ordinal != ordinal
    ):
        raise RunSwitchRetryLater(
            RunSwitchCode.CONTAINER_BUILD_PARENT_INVALID,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    if _stored_job_plan(job) != plan:
        raise RunSwitchRetryLater(
            RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID, reason=WaitReason.STALE_PLAN
        )
    current = _read_progress(read_row_column(job, "result"))
    if _bound_workload_intent(current) != ordinal:
        raise RunSwitchRetryLater(
            RunSwitchCode.CONTAINER_BUILD_PARENT_INVALID,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    if (
        not _checkpoint_matches(job, current, phase_index, item_index, child_id)
        or current.cancellation != cancellation
        or RetryHoldsMixin._scope_intent_status(session, job) != "current"
    ):
        raise _RunSwitchBuildParentChanged(RunSwitchCode.CONTAINER_BUILD_PARENT_CHANGED)
    return job


def _checkpoint_matches(
    job: Job,
    progress: RunSwitchOperationResult,
    phase_index: int,
    item_index: int,
    child_id: object,
) -> bool:
    """Accept an out-of-transaction observation only for its original checkpoint."""
    return (
        job.state in {LifecycleState.QUEUED.value, LifecycleState.RUNNING.value}
        and progress.phase_index == phase_index
        and progress.item_index == item_index
        and progress.child_operation_id == child_id
    )


_UNKNOWN_MEMBER_NODE_ID = "spk_" + "0" * 32
