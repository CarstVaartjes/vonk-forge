"""Job run completion for digest-bound recipe operations."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    LifecycleState,
    RecipeStopPayload,
    RunState,
    canonical_message,
)

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
    ProfileJobRunStopAuthorization,
    ProfileJobRunStopTarget,
    ProfileStopAuthorityError,
    validate_profile_jobrun_stop_target,
    validate_profile_stop_owner,
    validate_run_jobrun_stop_target,
)
from ..recipe_progress import (
    _parent_execution_mode as _parent_execution_mode,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _stored_phases as _stored_phases,  # noqa: PLC0414 -- shared helper export
)
from ..stored_json import read_row_column
from ..strict_json import read_stored_model
from .rank_authority import _profile_jobrun_parent

if TYPE_CHECKING:
    from .service import RecipeOperationService


class JobRunCompletionMixin:
    def _validate_profile_jobrun_stop_child(
        self,
        session: Session,
        job: Job,
        operation: AgentOperation,
        *,
        now: datetime,
        require_current: bool,
    ) -> tuple[ProfileJobRunStopAuthorization, ProfileJobRunStopTarget] | Residue:
        """The accepted authorization and target one Stop child executes.

        A child whose identity cannot be proven against its parent's accepted
        authority is retired as unknown (a :class:`Residue`): nothing is retired
        on that evidence and the profile's own retry answers the Stop.
        """

        def unproven(reason: BookkeepingReason, note: str) -> Residue:
            return retire_as_unknown(
                "recipe.profile-jobrun-stop", operation.id, reason, note
            )

        if (
            job.kind != WireAgentOperation.RECIPE_STOP.value
            or _parent_execution_mode(job) != "profile-jobrun-stop"
            or job.payload_digest
            != hashlib.sha256(
                canonical_message(read_row_column(job, "payload"))
            ).hexdigest()
        ):
            return unproven(
                BookkeepingReason.EVIDENCE_MISMATCH,
                "profile JobRun Stop parent is invalid",
            )
        parent = _profile_jobrun_parent(job)
        if isinstance(parent, Residue):
            return parent
        loaded_phases = _stored_phases(job)
        phase_matches = (
            []
            if isinstance(loaded_phases, Residue)
            else [
                payload
                for phase in loaded_phases
                for operation_id, node_id, payload in phase
                if operation_id == operation.id and node_id == operation.node_id
            ]
        )
        try:
            operation_payload = read_stored_model(
                RecipeStopPayload,
                canonical_message(read_row_column(operation, "payload")),
                from_json=True,
            )
        except (TypeError, ValueError) as error:
            return unproven(
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                f"profile JobRun Stop child payload is invalid: {error}",
            )
        stop_digest = hashlib.sha256(canonical_message(operation_payload)).hexdigest()
        targets = [
            target
            for target in parent.profile_stop_authorization.targets
            if target.node_id == operation.node_id
            and target.stop_payload_sha256 == stop_digest
        ]
        if (
            operation.parent_job_id != job.id
            or operation.kind != WireAgentOperation.RECIPE_STOP.value
            or operation.payload_digest
            != hashlib.sha256(
                canonical_message(read_row_column(operation, "payload"))
            ).hexdigest()
            or len(phase_matches) != 1
            or canonical_message(phase_matches[0])
            != canonical_message(operation_payload)
            or len(targets) != 1
        ):
            return unproven(
                BookkeepingReason.EVIDENCE_MISMATCH,
                "profile JobRun Stop child identity changed",
            )
        authorization = parent.profile_stop_authorization
        try:
            validate_profile_jobrun_stop_target(
                session,
                authorization,
                targets[0],
                operation_payload,
                operation=operation,
                stop_parent=job,
                now=now,
                require_current=require_current,
            )
        except ProfileStopAuthorityError as error:
            return unproven(
                BookkeepingReason.EVIDENCE_MISMATCH,
                f"profile JobRun Stop authority is stale: {error}",
            )
        return authorization, targets[0]

    def _complete_jobrun_stop_in_session(
        self,
        session: Session,
        job: Job,
        children: Sequence[AgentOperation],
        *,
        now: datetime,
        allow_pending: bool = False,
    ) -> Residue | None:
        """Retire old JobRun identities only after every exact Stop receipt.

        Every receipt is checked before anything is retired, so a receipt that
        cannot be proven (a changed payload, an owner no longer provable, a
        missing child or attempt) retires nothing: the whole completion returns a
        :class:`Residue` and the Stop ends failed, to be answered by the
        profile's own retry.  ``None`` means every identity was retired.
        """
        service = typing_cast("RecipeOperationService", self)

        def unproven(reason: BookkeepingReason, note: str) -> Residue:
            return retire_as_unknown("recipe.profile-jobrun-stop", job.id, reason, note)

        if (
            job.payload_digest
            != hashlib.sha256(
                canonical_message(read_row_column(job, "payload"))
            ).hexdigest()
        ):
            return unproven(
                BookkeepingReason.EVIDENCE_MISMATCH,
                "profile JobRun Stop payload digest changed",
            )
        is_profile = (
            getattr(read_row_column(job, "payload"), "execution_mode", None)
            == "profile-jobrun-stop"
        )
        if is_profile:
            parent = _profile_jobrun_parent(job)
            if isinstance(parent, Residue):
                return parent
            authorization: JobRunStopScope = parent.profile_stop_authorization
            try:
                validate_profile_stop_owner(
                    session,
                    parent.profile_stop_authorization,
                    now=now,
                    require_current=False,
                )
            except ProfileStopAuthorityError as error:
                return unproven(
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    f"profile JobRun Stop owner is no longer provable: {error}",
                )
        else:
            from ..job_documents import RecipeStopParent

            try:
                direct = RecipeStopParent.model_validate_json(
                    canonical_message(read_row_column(job, "payload")), strict=True
                )
                direct_scope = direct.job_run_stop_authorization
                if direct_scope is None:
                    return unproven(
                        BookkeepingReason.PERSISTED_STATE_DAMAGED,
                        "run JobRun Stop accepted scope is unreadable: exact JobRun Stop scope is missing",
                    )
                authorization = direct_scope
            except (TypeError, ValueError) as error:
                return unproven(
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    f"run JobRun Stop accepted scope is unreadable: {error}",
                )
        child_by_identity = {
            (
                child.node_id,
                hashlib.sha256(
                    canonical_message(read_row_column(child, "payload"))
                ).hexdigest(),
            ): child
            for child in children
        }
        if len(child_by_identity) != len(children) or any(
            child.state != LifecycleState.SUCCEEDED
            and not (allow_pending and child.node_id in deferred_stop_nodes(job))
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
            child = child_by_identity.get((target.node_id, target.stop_payload_sha256))
            if (
                allow_pending
                and target.node_id in deferred_stop_nodes(job)
                and child is not None
                and child.state != LifecycleState.SUCCEEDED
            ):
                continue
            if child is None or child.current_attempt < 1:
                return unproven(
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    "profile JobRun Stop lacks an exact issued receipt",
                )
            if is_profile:
                validated = service._validate_profile_jobrun_stop_child(
                    session, job, child, now=now, require_current=False
                )
                if isinstance(validated, Residue):
                    return validated
            else:
                try:
                    payload = read_stored_model(
                        RecipeStopPayload,
                        canonical_message(read_row_column(child, "payload")),
                        from_json=True,
                    )
                    validate_run_jobrun_stop_target(
                        session,
                        authorization,
                        target,
                        payload,
                        stop_parent=job,
                        operation=child,
                        require_current=False,
                    )
                except (TypeError, ValueError) as error:
                    return unproven(
                        BookkeepingReason.EVIDENCE_MISMATCH,
                        f"run JobRun Stop receipt authority changed: {error}",
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
        reachable_nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(
                    RunNode.run_id == authorization.run_id,
                    RunNode.node_id.in_(completed_nodes),
                )
                .with_for_update(of=RunNode)
            )
        )
        if {node.node_id for node in reachable_nodes} != completed_nodes:
            return unproven(
                BookkeepingReason.EVIDENCE_MISMATCH,
                "profile JobRun Stop reachable run membership changed",
            )
        reason = "runtime stopped by the newer accepted workload intent"
        for _target, artifact, source_job, source_operation in proven:
            # The exact Stop receipt proves the runtime absent: a definite,
            # confirmed cancellation of the job and of its order.
            ArtifactJobAdapter(session).confirm_stopped(artifact, reason, now)
            RecipeOperationAdapter().cancelled(
                source_job, now, reason=reason, keep=False
            )
            AgentOperationAdapter(session).record_outcome(
                source_operation,
                None,
                source_job,
                Outcome.CANCELLED,
                now,
                reason=reason,
            )
        for node in reachable_nodes:
            node.state = RunState.STOPPED
            node.updated_at = now
        return None
