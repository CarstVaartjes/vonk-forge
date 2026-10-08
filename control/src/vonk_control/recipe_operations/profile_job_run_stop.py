"""Profile job run stop for digest-bound recipe operations."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    RecipeJobRunRequest,
    RecipeStopPayload,
    RouteState,
    RunState,
    canonical_message,
)

from .. import artifact_job_states
from ..agent_jobs import (
    superseded_cancellation_deadline,
)
from ..categorized_errors import (
    InvalidValue,
)
from ..job_documents import (
    RunSwitchJobPayload,
    controller_recipe_document,
)
from ..lifecycle import CancelRequested, Effect, Outcome, Reported
from ..lifecycle.artifact_job import ArtifactJobAdapter
from ..lifecycle.evidence import (
    BookkeepingReason,
    Residue,
    retire_as_unknown,
)
from ..models import (
    AgentOperation,
    ArtifactJob,
    FleetProfileApplication,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..profile_stop_authority import (
    ProfileJobRunStopAuthorization,
    ProfileJobRunStopJob,
    ProfileJobRunStopTarget,
    ProfileStopAuthorityError,
    validate_profile_jobrun_stop_target,
    validate_profile_stop_owner,
)
from ..recipe_progress import (
    _parent_identity as _parent_identity,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_stop_payloads import (
    stop_payload_from_job_run,
)
from ..stored_json import read_row_column
from ..strict_json import read_stored_model
from .errors import (
    RecipeArtifactJobCancellationPending,
    RecipeRequestInvalid,
    RecipeStopAuthorityRefused,
)

if TYPE_CHECKING:
    from .service import RecipeOperationService


class ProfileJobRunStopMixin:
    def _profile_jobrun_stop_in_session(
        self,
        session: Session,
        *,
        run: RecipeRun,
        nodes: Sequence[RunNode],
        installation: RecipeInstallation,
        authority_revision: str,
        target_node_ids: Sequence[str],
        stop_plan_digest: str,
        actor: str,
        request_id: str,
        profile_application_id: str,
        workload_intent_ordinal: int,
        now: datetime,
    ) -> Job | Residue | None:
        """Queue exact JobRun runtime Stops under the accepted profile Stop.

        The accepted profile Stop is the authority for the JobRun runtime Stops.
        When its progress, owner or plan cannot be read, nothing is stopped on a
        guess: the damage is retired as unknown (a :class:`Residue`) and the
        caller's retry asks again.  A JobRun whose exact identity does not match
        the run is refused as a security edge, never stopped.
        """
        service = typing_cast("RecipeOperationService", self)
        from ..fleet_profiles import _persisted_profile_progress

        application = session.get(FleetProfileApplication, profile_application_id)
        try:
            switch_adapter = (
                _persisted_profile_progress(application).switch_adapter
                if application is not None
                else None
            )
        except ValueError as error:
            return retire_as_unknown(
                "recipe.profile-stop",
                profile_application_id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                f"current profile Stop progress is invalid: {error}",
            )
        matching_children = (
            [
                child
                for child in switch_adapter.pending_children
                if child.kind == "stop"
                and switch_adapter.queue[child.queue_index].id == run.id
            ]
            if switch_adapter is not None
            else []
        )
        profile_operation = (
            session.get(Job, matching_children[0].operation_id)
            if len(matching_children) == 1
            else None
        )
        if application is None or profile_operation is None:
            return retire_as_unknown(
                "recipe.profile-stop",
                profile_application_id,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "current profile Stop owner is unavailable",
            )
        try:
            profile_parent = read_stored_model(
                RunSwitchJobPayload,
                canonical_message(read_row_column(profile_operation, "payload")),
                strict=True,
                from_json=True,
            )
            run_switch_plan = profile_parent.plan
            stop_impacts = [
                item for item in run_switch_plan.stops if item.run_id == run.id
            ]
            expected_targets = (
                tuple(run_switch_plan.profile_stop_scope.target_node_ids)
                if run_switch_plan.profile_stop_scope is not None
                else tuple(node.node_id for node in nodes)
            )
            if len(stop_impacts) != 1 or tuple(sorted(target_node_ids)) != tuple(
                sorted(expected_targets)
            ):
                raise InvalidValue(
                    "accepted profile has no unique Stop for this run",
                    reason=InvalidRequestReason.NOT_FOUND,
                )
        except (TypeError, ValueError) as error:
            return retire_as_unknown(
                "recipe.profile-stop",
                profile_application_id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                f"accepted profile Stop plan is invalid: {error}",
            )

        run_node_by_id = {node.node_id: node for node in nodes}
        reachable_node_ids = set(target_node_ids)
        target_rows: list[
            tuple[ArtifactJob, Job, AgentOperation, RecipeStopPayload]
        ] = []
        unissued_artifacts: list[ArtifactJob] = []
        for artifact in session.scalars(
            select(ArtifactJob)
            .where(ArtifactJob.run_id == run.id)
            .order_by(ArtifactJob.created_at, ArtifactJob.id)
            .with_for_update(of=ArtifactJob)
        ):
            if artifact.operation_id is None:
                if artifact_job_states.preparation_of(artifact) is None:
                    # Submitted without an order: nothing was ever issued for
                    # it, so there is no runtime to stop and it is superseded
                    # like an unissued job.
                    retire_as_unknown(
                        "recipe.artifact-job",
                        artifact.id,
                        BookkeepingReason.ROW_INCOMPLETE,
                        "submitted artifact job has no JobRun identity",
                    )
                unissued_artifacts.append(artifact)
                continue
            source_job = session.get(Job, artifact.operation_id, with_for_update=True)
            source_operations = tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == artifact.operation_id)
                    .order_by(AgentOperation.node_id, AgentOperation.id)
                    .with_for_update(of=AgentOperation)
                )
            )
            if (
                source_job is None
                or source_job.kind != WireAgentOperation.RECIPE_JOB_RUN.value
                or _parent_identity(source_job, "owner_kind") != "artifact-job"
                or _parent_identity(source_job, "owner_id") != artifact.id
                or source_job.payload_digest
                != hashlib.sha256(
                    canonical_message(read_row_column(source_job, "payload"))
                ).hexdigest()
                or len(source_operations) != 1
                or source_operations[0].kind != WireAgentOperation.RECIPE_JOB_RUN.value
                or source_operations[0].parent_job_id != source_job.id
                or source_operations[0].payload_digest
                != hashlib.sha256(
                    canonical_message(read_row_column(source_operations[0], "payload"))
                ).hexdigest()
            ):
                raise RecipeStopAuthorityRefused(
                    "artifact JobRun physical Stop identity is ambiguous"
                )
            source_operation = source_operations[0]
            if source_operation.node_id not in run_node_by_id:
                raise RecipeStopAuthorityRefused(
                    "artifact JobRun escaped its immutable run membership"
                )
            if source_operation.node_id not in reachable_node_ids:
                continue
            source_node = run_node_by_id.get(source_operation.node_id)
            if source_node is None or source_job.targets != [source_operation.node_id]:
                raise RecipeStopAuthorityRefused(
                    "artifact JobRun escaped its immutable run membership"
                )
            try:
                request = read_stored_model(
                    RecipeJobRunRequest,
                    canonical_message(read_row_column(source_operation, "payload")),
                    from_json=True,
                )
                stop = stop_payload_from_job_run(
                    request,
                    cancel_pending_start=True,
                )
            except (TypeError, ValueError) as error:
                raise RecipeStopAuthorityRefused(
                    "artifact JobRun request cannot authorize exact Stop"
                ) from error
            if (
                request.job_id != artifact.id
                or request.run_id != run.id
                or request.installation_id != run.installation_id
                or request.recipe_revision_id != installation.recipe_revision_id
                or request.mapping_id != run.mapping_id
                or request.run_generation > run.run_generation
                or request.plan_digest != run.plan_digest
                or (
                    request.compiled_execution_plan.runtime.placement.rank,
                    request.compiled_execution_plan.runtime.placement.role,
                )
                != (source_node.rank, source_node.role)
                or stop.target_runtime_id != artifact.id
                or stop.cancel_pending_start is not True
            ):
                raise RecipeStopAuthorityRefused(
                    "artifact JobRun is not the exact current run effect"
                )
            target_rows.append((artifact, source_job, source_operation, stop))

        targets = [
            ProfileJobRunStopTarget(
                artifact_job_id=artifact.id,
                source_job_id=source_job.id,
                source_operation_id=source_operation.id,
                node_id=source_operation.node_id,
                stop_payload_sha256=hashlib.sha256(canonical_message(stop)).hexdigest(),
            )
            for artifact, source_job, source_operation, stop in target_rows
        ]
        try:
            authorization = ProfileJobRunStopAuthorization(
                schema_version=1,
                profile_application_id=application.id,
                profile_operation_id=profile_operation.id,
                profile_digest=application.profile_digest,
                profile_plan_digest=application.plan_digest,
                profile_step=application.current_step,
                run_id=run.id,
                installation_id=run.installation_id,
                recipe_revision_id=installation.recipe_revision_id,
                mapping_id=run.mapping_id,
                mapping_generation=run.mapping_generation,
                run_generation=run.run_generation,
                plan_digest=run.plan_digest,
                stop_plan_digest=stop_plan_digest,
                workload_intent_ordinal=workload_intent_ordinal,
                run_node_ids=[node.node_id for node in nodes],
                reachable_node_ids=sorted(reachable_node_ids),
                missing_node_ids=(
                    list(run_switch_plan.profile_stop_scope.missing_node_ids)
                    if run_switch_plan.profile_stop_scope is not None
                    else []
                ),
                targets=targets,
                unissued_artifact_job_ids=sorted(
                    artifact.id for artifact in unissued_artifacts
                ),
            )
            validate_profile_stop_owner(session, authorization, now=now)
            for target, row in zip(targets, target_rows, strict=True):
                validate_profile_jobrun_stop_target(
                    session, authorization, target, row[3], now=now
                )
        except (TypeError, ValueError, ProfileStopAuthorityError) as error:
            raise RecipeStopAuthorityRefused(
                "accepted profile does not authorize this exact JobRun Stop: "
                + str(error)[:240]
            ) from error

        for artifact in unissued_artifacts:
            ArtifactJobAdapter(session).settle(
                artifact,
                CancelRequested(None, "superseded before JobRun issuance"),
                now,
                reason="superseded before JobRun issuance by profile intent",
            )
        if not target_rows:
            return None

        grouped: dict[str, list[tuple[RecipeStopPayload, ProfileJobRunStopTarget]]] = {}
        for (_artifact, _source_job, source_operation, stop), target in zip(
            target_rows, targets, strict=True
        ):
            grouped.setdefault(source_operation.node_id, []).append((stop, target))
        for node_items in grouped.values():
            node_items.sort(
                key=lambda item: (item[1].artifact_job_id, item[1].source_operation_id)
            )
        phase_count = max(len(items) for items in grouped.values())
        phases = tuple(
            tuple(
                (node_id, grouped[node_id][index][0])
                for node_id in sorted(grouped)
                if index < len(grouped[node_id])
            )
            for index in range(phase_count)
        )
        node_payloads = tuple(
            (node_id, grouped[node_id][0][0]) for node_id in sorted(grouped)
        )
        run.state = RunState.STOPPING
        run.route_state = RouteState.WITHDRAWN
        run.route_error = "profile Stop is confirming every exact JobRun runtime"
        run.updated_at = now
        job = service._queue_in_session(
            session,
            kind=WireAgentOperation.RECIPE_STOP.value,
            owner_kind="run",
            owner_id=run.id,
            plan_digest=stop_plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=node_payloads,
            phases=phases,
            authority_digest=authority_revision,
            now=now,
            workload_intent_ordinal=workload_intent_ordinal,
            job_context={
                "execution_mode": "profile-jobrun-stop",
                "profile_application_id": application.id,
                "profile_operation_id": profile_operation.id,
                "profile_stop_authorization": controller_recipe_document(authorization),
            },
        )
        try:
            ProfileJobRunStopJob.model_validate_parent(
                ProfileJobRunStopJob.model_validate_json(
                    canonical_message(read_row_column(job, "payload"))
                ).model_dump(mode="json")
            )
        except (TypeError, ValueError) as error:
            raise RecipeStopAuthorityRefused(
                "profile JobRun Stop parent contract is invalid"
            ) from error
        return job

    @staticmethod
    def _one_shot_stop_prerequisite(
        session: Session,
        run_id: str,
        now: datetime,
        *,
        target_node_ids: Sequence[str] | None = None,
    ) -> RecipeArtifactJobCancellationPending | None:
        target_scope = set(target_node_ids) if target_node_ids is not None else None
        # An ended job never blocks a Stop: a job whose stop could not be confirmed
        # ended ``cancelled`` with its residue recorded in its evidence, and a
        # legacy ``failed`` job that lost its lease did too (core rule 4).  Only a
        # job still being cancelled waits, and it completes by itself.
        active = tuple(
            session.scalars(
                select(ArtifactJob)
                .where(
                    ArtifactJob.run_id == run_id,
                    artifact_job_states.sql_preparing_or_live(ArtifactJob),
                )
                .order_by(ArtifactJob.created_at, ArtifactJob.id)
                .with_for_update(of=ArtifactJob)
            )
        )
        pending: RecipeArtifactJobCancellationPending | None = None
        for artifact in active:
            if (
                target_scope is not None
                and not ProfileJobRunStopMixin._artifact_job_targets(
                    session, artifact, target_scope
                )
            ):
                continue
            if artifact.operation_id is None:
                if artifact_job_states.preparation_of(artifact) is None:
                    # Submitted without an order: nothing was ever issued for it,
                    # so it is superseded like an unissued job.
                    retire_as_unknown(
                        "recipe.artifact-job",
                        artifact.id,
                        BookkeepingReason.ROW_INCOMPLETE,
                        "submitted artifact job has no operation identity",
                    )
                ArtifactJobAdapter(session).settle(
                    artifact,
                    CancelRequested(None, "superseded by newer workload intent"),
                    now,
                    reason="superseded by newer workload intent",
                )
                continue
            parent = session.get(Job, artifact.operation_id, with_for_update=True)
            children = tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == artifact.operation_id)
                    .with_for_update(of=AgentOperation)
                )
            )
            if (
                parent is None
                or parent.kind != WireAgentOperation.RECIPE_JOB_RUN.value
                or _parent_identity(parent, "owner_id") != artifact.id
                or len(children) != 1
                or children[0].node_id not in parent.targets
            ):
                raise RecipeRequestInvalid(
                    "artifact job cancellation authority changed",
                    reason=InvalidRequestReason.CONFLICT,
                )
            if (
                parent.state == LifecycleState.CANCELLED.value
                and children[0].state == LifecycleState.CANCELLED.value
                and children[0].current_attempt == 0
            ):
                ArtifactJobAdapter(session).settle(
                    artifact,
                    Reported(
                        Outcome.CANCELLED,
                        effect=Effect.NONE,
                        reason="superseded before agent dispatch",
                    ),
                    now,
                    reason="superseded before agent dispatch",
                )
                continue
            deadline = superseded_cancellation_deadline(
                read_row_column(parent, "result")
            )
            if deadline is None:
                raise RecipeRequestInvalid(
                    "artifact job has no cancellation authority",
                    reason=InvalidRequestReason.NOT_FOUND,
                )
            # The order carries the superseding cancel: the job mirrors it
            # (``cancelling``, or ended already) and reports the wait.
            ArtifactJobAdapter(session).project(
                artifact,
                now,
                reason="waiting for exact artifact cancellation receipt",
            )
            if pending is None:
                pending = RecipeArtifactJobCancellationPending(
                    job_id=parent.id,
                    observe_due_at=now + timedelta(seconds=5),
                    observation_deadline=deadline,
                )
        return pending

    @staticmethod
    def _artifact_job_targets(
        session: Session, artifact: ArtifactJob, target_node_ids: set[str]
    ) -> bool:
        if artifact.operation_id is None:
            return False
        parent = session.get(Job, artifact.operation_id)
        return bool(parent is not None and set(parent.targets) & target_node_ids)
