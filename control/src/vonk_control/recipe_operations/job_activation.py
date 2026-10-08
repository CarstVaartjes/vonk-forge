"""Job activation for digest-bound recipe operations."""

from __future__ import annotations

import hashlib
import uuid
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    RouteState,
    RunState,
    UnknownOutcomeError,
    canonical_message,
)

from ..admission_locking import (
    AdmissionLockBusy,
    acquire_admission_keys,
    admission_attempts,
    admission_wait_exhausted,
    job_request_key,
)
from ..agent_jobs import (
    AgentJobService,
)
from ..models import (
    AgentNode,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_run_plan,
    run_plan_document,
)
from ..recipe_progress import _catalog_recipe
from ..run_admission import (
    RunAdmissionBusy,
    RunPlan,
)
from ..run_admission import (
    require_admissible as require_run_admissible,
)
from ..run_admission import (
    require_same_execution as require_same_run_execution,
)
from ..stored_json import read_row_column
from ..strict_json import serialize_json_value
from .errors import RecipeRequestInvalid
from .interfaces import RecipeOperationView, new_recipe_job
from .observation_helpers import _active_recipe_revision
from .results import _validated_result

if TYPE_CHECKING:
    from .service import RecipeOperationService


class JobActivationMixin:
    def _activate_job_run_once(
        self,
        plan: RunPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
    ) -> RecipeOperationView:
        service = typing_cast("RecipeOperationService", self)
        if plan_digest != plan.plan_digest:
            raise RecipeRequestInvalid(
                "reviewed plan digest does not match the submitted plan",
                reason=InvalidRequestReason.CONFLICT,
            )
        reviewed = plan
        existing = service._idempotent(
            request_id,
            "recipe.job.activate.v1",
            None,
            installation_id=plan.installation_id,
            run_alias=plan.alias,
        )
        if existing is not None:
            return existing
        now = service._clock()
        with service._sessions.begin() as session:
            try:
                acquire_admission_keys(session, (job_request_key(request_id),))
            except AdmissionLockBusy as error:
                raise RunAdmissionBusy("run capacity writer is busy") from error
            replay = service._idempotent_in_session(
                session,
                request_id,
                "recipe.job.activate.v1",
                None,
                installation_id=plan.installation_id,
                run_alias=plan.alias,
            )
            if replay is not None:
                return replay
            plan = service._run_admission.plan_run(
                plan.installation_id, plan.alias, now=now, _session=session
            )
            require_run_admissible(plan)
            require_same_run_execution(reviewed, plan)
            installation = session.get(RecipeInstallation, plan.installation_id)
            revision = (
                _active_recipe_revision(session, installation.recipe_revision_id)
                if installation is not None
                else None
            )
            recipe = None
            if revision is not None:
                try:
                    recipe = _catalog_recipe(read_row_column(revision, "document"))
                except (TypeError, ValueError):
                    pass
            adapters = (
                [interface.adapter for interface in recipe.interfaces]
                if recipe is not None
                else []
            )
            artifact_adapters = {
                "audio-job",
                "video-job",
                "image-job",
                "mesh-job",
                "artifact-job",
            }
            if (
                recipe is None
                or len(adapters) != 1
                or adapters[0] not in artifact_adapters
            ):
                raise RecipeRequestInvalid("recipe is not an artifact job recipe")
            if recipe.topology.node_count != 1:
                raise RecipeRequestInvalid(
                    "artifact job recipes currently require a single-node topology"
                )
            try:
                run_id = service._run_admission.accept_run_in_session(
                    session, plan, actor=actor, now=now
                )
            except (RuntimeError, ValueError) as error:
                raise RecipeRequestInvalid(str(error)) from error
            run = session.get(RecipeRun, run_id)
            assert run is not None and revision is not None
            run.state = RunState.RUNNING
            run.route_state = RouteState.WITHDRAWN
            # The run row was written by this very transaction: its plan must
            # accept the one-shot mode before anything can commit it.
            try:
                updated_plan = parse_stored_run_plan(
                    read_row_column(run, "plan")
                ).model_copy(update={"execution_mode": "one-shot-jobs"})
                run.plan = run_plan_document(updated_plan)
            except RecipeExecutionContractError as error:
                raise RecipeRequestInvalid("stored run plan is invalid") from error
            run.updated_at = now
            nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            )
            for node in nodes:
                node.state = RunState.RUNNING
                node.updated_at = now
            targets = sorted(node.node_id for node in nodes)
            target_nodes = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(targets))
                    .order_by(AgentNode.node_id)
                    .with_for_update(of=AgentNode)
                )
            )
            if tuple(node.node_id for node in target_nodes) != tuple(targets):
                raise RecipeRequestInvalid("artifact workload target disappeared")
            workload_intent_ordinal = (
                max(node.workload_intent_ordinal for node in target_nodes) + 1
            )
            for node in target_nodes:
                node.workload_intent_ordinal = workload_intent_ordinal
            AgentJobService.request_superseded_workload_cancellation_in_session(
                session, targets, workload_intent_ordinal, now
            )
            payload = {
                "schema_version": 1,
                "owner_kind": "run",
                "owner_id": run_id,
                "plan_digest": plan.plan_digest,
                "execution_mode": "one-shot-jobs",
                "workload_intent_ordinal": workload_intent_ordinal,
            }
            job = new_recipe_job(
                id=str(uuid.uuid4()),
                request_id=request_id,
                kind="recipe.job.activate.v1",
                state=LifecycleState.SUCCEEDED.value,
                actor=actor,
                authority_revision=revision.content_digest or "",
                targets=targets,
                payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
                payload=payload,
                result=serialize_json_value(
                    _validated_result("recipe.job.activate.v1", {"activated": True})
                ),
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()
            return service._view(job)

    def activate_job_run(
        self,
        plan: RunPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
    ) -> RecipeOperationView:
        """Reserve an installed artifact recipe without starting a service container."""
        service = typing_cast("RecipeOperationService", self)
        refused: UnknownOutcomeError | None = None
        for _attempt in admission_attempts():
            try:
                return service._activate_job_run_once(
                    plan,
                    plan_digest=plan_digest,
                    actor=actor,
                    request_id=request_id,
                )
            except UnknownOutcomeError as error:
                refused = error
                if admission_wait_exhausted(error):
                    break
        assert refused is not None
        raise refused
