"""Job run completion for digest-bound recipe operations."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    TERMINAL_LIFECYCLE_STATES,
    LifecycleState,
    RecipeStopPayload,
    RunState,
    WaitReason,
    canonical_message,
)

from .. import agent_operation_states as aos
from .. import job_states
from ..lifecycle import Outcome
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..lifecycle.artifact_job import ArtifactJobAdapter
from ..lifecycle.evidence import (
    BookkeepingReason,
    Residue,
    retire_as_unknown,
)
from ..lifecycle.recipe_operation import RecipeOperationAdapter
from ..models import (
    AgentOperation,
    AgentOperationAttempt,
    ArtifactJob,
    Job,
    RunNode,
)
from ..offline_stops import deferred_stop_nodes
from ..profile_stop_authority import (
    JobRunStopScope,
    ProfileJobRunStopTarget,
    jobrun_stop_key,
)
from ..recipe_progress import _parent_execution_mode
from ..stored_json import read_row_column
from .rank_authority import _profile_jobrun_parent


class JobRunCompletionMixin:
    def _complete_jobrun_stop_in_session(
        self,
        session: Session,
        job: Job,
        children: Sequence[AgentOperation],
        *,
        now: datetime,
        allow_pending: bool = False,
    ) -> Residue | None:
        """Retire the stopped JobRun identities once every Stop has its receipt.

        A successful receipt of the Stop's own order (its child, on its Spark,
        at its current attempt) proves the job container gone. Each order is
        matched to its target by identity: the Spark and the container it
        names. What the run, the profile or the rows' digests became since it
        was issued never refuses the receipt. A parent whose targets cannot be
        read, or a target without a successful receipt, retires nothing: the
        whole completion returns a :class:`Residue` and the Stop ends failed.
        ``None`` means every identity was retired.
        """

        def unproven(reason: BookkeepingReason, note: str) -> Residue:
            return retire_as_unknown("recipe.profile-jobrun-stop", job.id, reason, note)

        if _parent_execution_mode(job) == "profile-jobrun-stop":
            parent = _profile_jobrun_parent(job)
            if isinstance(parent, Residue):
                return parent
            authorization: JobRunStopScope = parent.profile_stop_authorization
        else:
            from ..job_documents import RecipeStopParent

            try:
                direct = RecipeStopParent.model_validate_json(
                    canonical_message(read_row_column(job, "payload")), strict=True
                )
            except (TypeError, ValueError) as error:
                return unproven(
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    f"run JobRun Stop accepted scope is unreadable: {error}",
                )
            if direct.job_run_stop_authorization is None:
                return unproven(
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    "run JobRun Stop accepted scope is unreadable: exact JobRun Stop scope is missing",
                )
            authorization = direct.job_run_stop_authorization
        child_by_key: dict[tuple[str, str], AgentOperation] = {}
        for child in children:
            stop = read_row_column(child, "payload")
            if not isinstance(stop, RecipeStopPayload):
                return unproven(
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    "JobRun Stop order is unreadable",
                )
            child_by_key[jobrun_stop_key(child.node_id, stop)] = child
        deferred = deferred_stop_nodes(job) if allow_pending else frozenset()
        if len(child_by_key) != len(children) or any(
            child.state != LifecycleState.SUCCEEDED and child.node_id not in deferred
            for child in children
        ):
            return unproven(
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "profile JobRun Stop lacks complete successful child receipts",
            )
        proven: list[
            tuple[ProfileJobRunStopTarget, ArtifactJob, Job, AgentOperation]
        ] = []
        for target in authorization.targets:
            child = child_by_key.get(target.key)
            if (
                target.node_id in deferred
                and child is not None
                and child.state != LifecycleState.SUCCEEDED
            ):
                continue
            if child is None or child.current_attempt < 1:
                return unproven(
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    "profile JobRun Stop lacks an exact issued receipt",
                )
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == child.id,
                    AgentOperationAttempt.attempt == child.current_attempt,
                )
            )
            if attempt is None or attempt.state != LifecycleState.SUCCEEDED:
                return unproven(
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    "profile JobRun Stop result does not prove absence",
                )
            artifact = session.get(
                ArtifactJob, target.artifact_job_id, with_for_update=True
            )
            source_job = session.get(Job, target.source_job_id, with_for_update=True)
            source_operation = session.get(
                AgentOperation, target.source_operation_id, with_for_update=True
            )
            if (
                artifact is None
                or source_job is None
                or source_operation is None
                or artifact.run_id != authorization.run_id
                or artifact.operation_id != source_job.id
                or source_operation.parent_job_id != source_job.id
            ):
                return unproven(
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "stopped JobRun source no longer matches its owner",
                )
            proven.append((target, artifact, source_job, source_operation))
        completed_nodes = (
            {target.node_id for target, _artifact, _source, _operation in proven}
            if allow_pending
            else set(authorization.reachable_node_ids)
        )
        reason = WaitReason.SCOPE_CHANGED
        for _target, artifact, source_job, source_operation in proven:
            # The exact Stop receipt proves the runtime absent: a definite,
            # confirmed cancellation of every row still live.  A row that
            # already ended keeps its recorded outcome; for the job the receipt
            # resolves only what it may have left on the Spark.
            ArtifactJobAdapter(session).confirm_stopped(artifact, reason, now)
            if not job_states.means(source_job.state, *TERMINAL_LIFECYCLE_STATES):
                RecipeOperationAdapter().cancelled(
                    source_job, now, reason=reason, keep=False
                )
            if source_operation.state in aos.LIVE:
                AgentOperationAdapter(session).record_outcome(
                    source_operation,
                    None,
                    source_job,
                    Outcome.CANCELLED,
                    now,
                    reason=reason,
                )
        for node in session.scalars(
            select(RunNode)
            .where(
                RunNode.run_id == authorization.run_id,
                RunNode.node_id.in_(completed_nodes),
            )
            .with_for_update(of=RunNode)
        ):
            node.state = RunState.STOPPED
            node.updated_at = now
        return None
