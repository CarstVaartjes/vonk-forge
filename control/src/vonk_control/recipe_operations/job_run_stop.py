"""Job run stop for digest-bound recipe operations."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import datetime
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

from ..agent_jobs import (
    AgentJobService,
)
from ..artifact_job_evidence import ArtifactJobResultEvidence
from ..job_documents import RecipeStopParent, controller_recipe_document
from ..lifecycle import Effect
from ..lifecycle.artifact_job import ArtifactJobAdapter
from ..lifecycle.evidence import (
    Residue,
)
from ..lifecycle.recipe_operation import RecipeOperationAdapter
from ..models import (
    AgentNode,
    AgentOperation,
    ArtifactJob,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..profile_stop_authority import (
    JobRunStopScope,
    ProfileJobRunStopJob,
    ProfileJobRunStopTarget,
    ProfileStopAuthorityError,
    validate_jobrun_stop_source,
    validate_profile_jobrun_stop_target,
    validate_profile_stop_owner,
    validate_run_jobrun_stop_target,
)
from ..recipe_progress import (
    _parent_execution_mode as _parent_execution_mode,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _parent_identity as _parent_identity,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_stop_payloads import (
    stop_payload_from_job_run,
)
from ..stored_json import read_row_column
from ..strict_json import read_stored_model, serialize_json_value
from .errors import (
    RecipeArtifactJobCancellationPending,
    RecipeRequestInvalid,
    RecipeRetryLater,
    RecipeStopAuthorityRefused,
)
from .intent import _bound_workload_intent
from .interfaces import RecipeOperationView, new_recipe_job
from .observation_helpers import _active_recipe_revision
from .rank_authority import _profile_jobrun_parent, _run_is_one_shot
from .results import _validated_result

if TYPE_CHECKING:
    from .service import RecipeOperationService


class JobRunStopMixin:
    def _stop_logical_job_run(
        self,
        run_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None,
        profile_target_node_ids: Sequence[str] | None = None,
        profile_application_id: str | None = None,
    ) -> RecipeOperationView | RecipeArtifactJobCancellationPending | None:
        service = typing_cast("RecipeOperationService", self)
        now = service._clock()
        with service._sessions() as session:
            existing_run = session.get(RecipeRun, run_id)
            if existing_run is None or not _run_is_one_shot(session, existing_run):
                return None
            if profile_target_node_ids is not None and profile_application_id is None:
                raise RecipeRequestInvalid(
                    "partial one-shot Stop requires current profile ownership"
                )
        with service._sessions.begin() as session:
            pending_job = session.scalar(
                select(Job).where(Job.request_id == request_id).with_for_update(of=Job)
            )
            if pending_job is not None:
                if _parent_execution_mode(pending_job) == "profile-jobrun-stop":
                    if (
                        profile_application_id is None
                        or pending_job.state != LifecycleState.RUNNING.value
                    ):
                        raise RecipeRequestInvalid(
                            "profile JobRun Stop request identity changed"
                        )
                    typed_parent = _profile_jobrun_parent(pending_job)
                    if isinstance(typed_parent, Residue):
                        # The Stop this request key already queued is in flight
                        # under its own orders; its damaged parent cannot be
                        # re-checked here, so the request adopts it (its result
                        # projection settles it).
                        return service._view(pending_job, session=session)
                    authorization = typed_parent.profile_stop_authorization
                    if (
                        typed_parent.profile_application_id != profile_application_id
                        or typed_parent.owner_id != run_id
                        or typed_parent.plan_digest != plan_digest
                        or typed_parent.workload_intent_ordinal
                        != workload_intent_ordinal
                    ):
                        raise RecipeRequestInvalid(
                            "profile JobRun Stop request changed its owner",
                            reason=InvalidRequestReason.CONFLICT,
                        )
                    try:
                        validate_profile_stop_owner(session, authorization, now=now)
                        for item in typed_parent.flattened_phase_items:
                            target = next(
                                target
                                for target in authorization.targets
                                if target.node_id == item.node_id
                                and target.stop_payload_sha256
                                == hashlib.sha256(
                                    canonical_message(item.payload)
                                ).hexdigest()
                            )
                            validate_profile_jobrun_stop_target(
                                session,
                                authorization,
                                target,
                                item.payload,
                                stop_parent=pending_job,
                                now=now,
                            )
                    except (ProfileStopAuthorityError, StopIteration) as error:
                        raise RecipeRequestInvalid(
                            "profile JobRun Stop authority is stale",
                            reason=InvalidRequestReason.SUPERSEDED,
                        ) from error
                    return service._view(pending_job, session=session)
                if (
                    pending_job.kind != WireAgentOperation.RECIPE_STOP.value
                    or pending_job.state != LifecycleState.RUNNING.value
                    or _parent_identity(pending_job, "owner_id") != run_id
                    or _parent_execution_mode(pending_job) != "one-shot-jobs"
                ):
                    raise RecipeRequestInvalid(
                        "request key was already used differently",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                bound = _bound_workload_intent(pending_job)
                if (
                    workload_intent_ordinal is not None
                    and workload_intent_ordinal != bound
                ):
                    raise RecipeRequestInvalid("workload intent was superseded")
                workload_intent_ordinal = bound
            run = session.get(RecipeRun, run_id, with_for_update=True)
            if run is None or not _run_is_one_shot(session, run):
                raise RecipeRequestInvalid(
                    "logical recipe run changed while stopping",
                    reason=InvalidRequestReason.CONFLICT,
                )
            admitted = service._stop_plan_in_session(
                session,
                run_id,
                lock=True,
                profile_target_node_ids=profile_target_node_ids,
            )
            if not admitted.allowed:
                raise RecipeRequestInvalid("stop plan is stale or blocked")
            if pending_job is None and plan_digest != admitted.plan_digest:
                raise RecipeRequestInvalid(
                    "reviewed Stop plan digest does not match current effects",
                    reason=InvalidRequestReason.CONFLICT,
                )
            plan_digest = admitted.plan_digest
            installation = session.get(RecipeInstallation, run.installation_id)
            if installation is None:
                raise RecipeRequestInvalid("recipe installation does not exist")
            revision = _active_recipe_revision(session, installation.recipe_revision_id)
            # The accepted Stop plan carries the run's own authority; the recipe
            # revision's digest is used when it is readable, never required.
            authority_revision = (
                revision.content_digest
                if revision is not None and revision.content_digest
                else admitted.authority_digest.removeprefix("sha256:")
            )
            nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            )
            targets = admitted.target_node_ids
            target_nodes = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(targets))
                    .order_by(AgentNode.node_id)
                    .with_for_update(of=AgentNode)
                )
            )
            if tuple(node.node_id for node in target_nodes) != targets:
                raise RecipeRequestInvalid("artifact workload target disappeared")
            if workload_intent_ordinal is None:
                workload_intent_ordinal = (
                    max(node.workload_intent_ordinal for node in target_nodes) + 1
                )
                for node in target_nodes:
                    node.workload_intent_ordinal = workload_intent_ordinal
                AgentJobService.request_superseded_workload_cancellation_in_session(
                    session, targets, workload_intent_ordinal, now
                )
            elif (
                type(workload_intent_ordinal) is not int
                or workload_intent_ordinal < 1
                or any(
                    node.workload_intent_ordinal != workload_intent_ordinal
                    for node in target_nodes
                )
            ):
                raise RecipeRequestInvalid("workload intent was superseded")
            if profile_application_id is not None:
                profile_job = service._profile_jobrun_stop_in_session(
                    session,
                    run=run,
                    nodes=nodes,
                    installation=installation,
                    authority_revision=authority_revision,
                    target_node_ids=targets,
                    stop_plan_digest=admitted.plan_digest,
                    actor=actor,
                    request_id=request_id,
                    profile_application_id=profile_application_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                    now=now,
                )
                if isinstance(profile_job, Residue):
                    # The accepted profile Stop cannot be read yet: nothing is
                    # stopped on a guess, and the profile's retry asks again.
                    raise RecipeRetryLater(
                        "the accepted profile Stop is not readable yet; it is "
                        "recorded and the stop is retried"
                    )
                if profile_job is not None:
                    return service._view(profile_job, session=session)
            else:
                exact_job = service._run_jobrun_stop_in_session(
                    session,
                    run=run,
                    nodes=nodes,
                    target_node_ids=targets,
                    stop_plan_digest=plan_digest,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                    now=now,
                    pending_job=pending_job,
                )
                if isinstance(exact_job, RecipeArtifactJobCancellationPending):
                    return exact_job
                if exact_job is not None:
                    return service._view(exact_job, session=session)
            payload = {
                "schema_version": 1,
                "owner_kind": "run",
                "owner_id": run_id,
                "plan_digest": plan_digest,
                "execution_mode": "one-shot-jobs",
                "workload_intent_ordinal": workload_intent_ordinal,
            }
            pending = service._one_shot_stop_prerequisite(
                session,
                run_id,
                now,
                target_node_ids=targets if admitted.missing_node_ids else None,
            )
            if pending is not None:
                if pending_job is None:
                    session.add(
                        new_recipe_job(
                            id=str(uuid.uuid4()),
                            request_id=request_id,
                            kind=WireAgentOperation.RECIPE_STOP.value,
                            state=LifecycleState.RUNNING.value,
                            actor=actor,
                            authority_revision=authority_revision,
                            targets=list(targets),
                            payload_digest=hashlib.sha256(
                                canonical_message(payload)
                            ).hexdigest(),
                            payload=controller_recipe_document(
                                RecipeStopParent.model_validate_json(
                                    canonical_message(payload)
                                )
                            ),
                            result=None,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    session.flush()
                return pending
            for node in nodes:
                if node.node_id in targets:
                    node.state = RunState.STOPPED
                    node.updated_at = now
            run.state = RunState.LOST if admitted.missing_node_ids else RunState.STOPPED
            run.route_state = RouteState.WITHDRAWN
            run.stopped_at = None if admitted.missing_node_ids else now
            if admitted.missing_node_ids:
                run.route_error = (
                    "incomplete multi-Spark model; missing ranks were not stopped"
                )
            run.updated_at = now
            if admitted.missing_node_ids:
                service._release_node_reservations(session, run_id, targets, now)
            else:
                service._release(session, "run", run_id, now)
            job = pending_job or new_recipe_job(
                id=str(uuid.uuid4()),
                request_id=request_id,
                kind=WireAgentOperation.RECIPE_STOP.value,
                state=LifecycleState.SUCCEEDED.value,
                actor=actor,
                authority_revision=authority_revision,
                targets=list(targets),
                payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
                payload=controller_recipe_document(
                    RecipeStopParent.model_validate_json(canonical_message(payload))
                ),
                result=serialize_json_value(
                    _validated_result(
                        WireAgentOperation.RECIPE_STOP.value,
                        {RunState.STOPPED.value: True},
                    )
                ),
                created_at=now,
                updated_at=now,
            )
            if pending_job is not None:
                RecipeOperationAdapter().finish(pending_job, now, failed=False)
                pending_job.result = serialize_json_value(
                    _validated_result(
                        WireAgentOperation.RECIPE_STOP.value,
                        {RunState.STOPPED.value: True},
                    )
                )
                pending_job.updated_at = now
            else:
                session.add(job)
            session.flush()
            return service._view(job)

    def _one_shot_stop_is_pending(self, request_id: str) -> bool:
        service = typing_cast("RecipeOperationService", self)
        with service._sessions() as session:
            job = session.scalar(select(Job).where(Job.request_id == request_id))
            return bool(
                job is not None
                and job.kind == WireAgentOperation.RECIPE_STOP.value
                and job.state == LifecycleState.RUNNING.value
                and _parent_execution_mode(job) == "one-shot-jobs"
                and getattr(read_row_column(job, "payload"), "phases", None) is None
            )

    def _run_jobrun_stop_in_session(
        self,
        session: Session,
        *,
        run: RecipeRun,
        nodes: Sequence[RunNode],
        target_node_ids: Sequence[str],
        stop_plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int,
        now: datetime,
        pending_job: Job | None,
    ) -> Job | RecipeArtifactJobCancellationPending | None:
        """Freeze exact issued targets before admitting ordinary run cleanup."""
        service = typing_cast("RecipeOperationService", self)
        rows: list[tuple[ProfileJobRunStopTarget, RecipeStopPayload]] = []
        adapter = ArtifactJobAdapter(session)
        frozen: JobRunStopScope | None = None
        if pending_job is not None:
            frozen = RecipeStopParent.model_validate_json(
                canonical_message(read_row_column(pending_job, "payload")), strict=True
            ).job_run_stop_authorization
        frozen_ids = (
            {target.artifact_job_id for target in frozen.targets}
            if frozen is not None
            else None
        )
        needs_stop = False
        for artifact in session.scalars(
            select(ArtifactJob)
            .where(ArtifactJob.run_id == run.id)
            .order_by(ArtifactJob.created_at, ArtifactJob.id)
            .with_for_update(of=ArtifactJob)
        ):
            effect = adapter.adopt(artifact).effect
            evidence = read_row_column(artifact, "result_evidence")
            if isinstance(evidence, Residue):
                # A damaged receipt is uncertainty about the physical effect;
                # it must never be adopted as an empty successful receipt.
                effect = Effect.UNKNOWN
            elif (
                isinstance(evidence, ArtifactJobResultEvidence)
                and evidence.active_scope_may_remain is True
            ):
                effect = Effect.UNKNOWN
            if frozen_ids is not None:
                if artifact.id not in frozen_ids:
                    if effect in {Effect.ISSUED, Effect.UNKNOWN}:
                        raise RecipeStopAuthorityRefused(
                            "new JobRun effect escaped the accepted Stop scope"
                        )
                    continue
            elif effect not in {Effect.ISSUED, Effect.UNKNOWN}:
                continue
            needs_stop |= effect in {Effect.ISSUED, Effect.UNKNOWN}
            if artifact.operation_id is None:
                raise RecipeRetryLater("unknown JobRun has no exact source authority")
            children = tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == artifact.operation_id)
                    .with_for_update(of=AgentOperation)
                )
            )
            if len(children) != 1 or children[0].node_id not in target_node_ids:
                raise RecipeRetryLater("unknown JobRun exact target is not reachable")
            source = children[0]
            request = read_stored_model(
                RecipeJobRunRequest,
                canonical_message(read_row_column(source, "payload")),
                from_json=True,
            )
            payload = stop_payload_from_job_run(request, cancel_pending_start=True)
            target = ProfileJobRunStopTarget(
                artifact_job_id=artifact.id,
                source_job_id=artifact.operation_id,
                source_operation_id=source.id,
                node_id=source.node_id,
                stop_payload_sha256=hashlib.sha256(
                    canonical_message(payload)
                ).hexdigest(),
            )
            validate_jobrun_stop_source(
                session,
                target,
                payload,
                run,
                workload_intent_ordinal=workload_intent_ordinal,
            )
            rows.append((target, payload))
        if frozen_ids is not None and frozen_ids != {
            target.artifact_job_id for target, _payload in rows
        }:
            raise RecipeRetryLater("accepted JobRun Stop source disappeared")
        if not rows:
            return None
        installation = session.get(RecipeInstallation, run.installation_id)
        if installation is None:
            raise RecipeRetryLater("exact JobRun installation authority is unavailable")
        scope = JobRunStopScope(
            schema_version=1,
            run_id=run.id,
            installation_id=run.installation_id,
            recipe_revision_id=installation.recipe_revision_id,
            mapping_id=run.mapping_id,
            mapping_generation=run.mapping_generation,
            run_generation=run.run_generation,
            plan_digest=run.plan_digest,
            workload_intent_ordinal=workload_intent_ordinal,
            run_node_ids=[node.node_id for node in nodes],
            reachable_node_ids=sorted(target_node_ids),
            missing_node_ids=sorted(
                node.node_id for node in nodes if node.node_id not in target_node_ids
            ),
            targets=[target for target, _payload in rows],
            stop_plan_digest=stop_plan_digest,
        )
        if frozen is not None and frozen != scope:
            raise RecipeStopAuthorityRefused(
                "accepted exact JobRun Stop targets changed"
            )
        if not needs_stop:
            # Each original source has now positively reported a stopped/ended
            # effect. The frozen scope remains exact; no unseen target is added.
            return None
        pending = service._one_shot_stop_prerequisite(
            session, run.id, now, target_node_ids=target_node_ids
        )
        if pending is not None:
            if pending_job is None:
                payload = {
                    "schema_version": 1,
                    "owner_kind": "run",
                    "owner_id": run.id,
                    "plan_digest": stop_plan_digest,
                    "execution_mode": "one-shot-jobs",
                    "workload_intent_ordinal": workload_intent_ordinal,
                    "job_run_stop_authorization": controller_recipe_document(scope),
                }
                session.add(
                    new_recipe_job(
                        id=str(uuid.uuid4()),
                        request_id=request_id,
                        kind=WireAgentOperation.RECIPE_STOP.value,
                        state=LifecycleState.RUNNING.value,
                        actor=actor,
                        authority_revision=rows[0][1].recipe_content_sha256,
                        targets=sorted(target_node_ids),
                        payload_digest=hashlib.sha256(
                            canonical_message(payload)
                        ).hexdigest(),
                        payload=controller_recipe_document(
                            RecipeStopParent.model_validate_json(
                                canonical_message(payload)
                            )
                        ),
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.flush()
            return pending
        # One-shot recipes are single-node. Sequential phases bind each distinct
        # target without creating concurrent cleanup claims on that node.
        phases = tuple(
            ((target.node_id, json.loads(canonical_message(payload))),)
            for target, payload in rows
        )
        run.state = RunState.STOPPING
        run.updated_at = now
        parent = service._queue_in_session(
            session,
            kind=WireAgentOperation.RECIPE_STOP.value,
            owner_kind="run",
            owner_id=run.id,
            plan_digest=stop_plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=phases[0],
            phases=phases,
            authority_digest=rows[0][1].recipe_content_sha256,
            now=now,
            workload_intent_ordinal=workload_intent_ordinal,
            job_context={
                "execution_mode": "one-shot-jobs",
                "job_run_stop_authorization": controller_recipe_document(scope),
            },
            adopt_stop_parent=pending_job,
        )
        for target, payload in rows:
            validate_run_jobrun_stop_target(
                session, scope, target, payload, stop_parent=parent
            )
        return parent

    def _profile_jobrun_stop_is_pending(self, request_id: str) -> bool:
        service = typing_cast("RecipeOperationService", self)
        with service._sessions() as session:
            job = session.scalar(select(Job).where(Job.request_id == request_id))
            if (
                job is None
                or job.kind != WireAgentOperation.RECIPE_STOP.value
                or job.state != LifecycleState.RUNNING.value
                or _parent_execution_mode(job) != "profile-jobrun-stop"
            ):
                return False
            try:
                ProfileJobRunStopJob.model_validate_parent(
                    ProfileJobRunStopJob.model_validate_json(
                        canonical_message(read_row_column(job, "payload"))
                    ).model_dump(mode="json")
                )
            except (TypeError, ValueError):
                return False
            return True
