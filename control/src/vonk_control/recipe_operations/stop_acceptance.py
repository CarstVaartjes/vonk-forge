"""Stop acceptance for digest-bound recipe operations."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    RouteState,
    canonical_message,
)

from ..admission_locking import (
    acquire_admission_keys,
    job_request_key,
)
from ..job_documents import (
    RecipeStopParent,
    RunSwitchJobPayload,
    ServiceRunStopReview,
    controller_recipe_document,
)
from ..logging import redact_text
from ..models import (
    FleetProfileApplication,
    Job,
    RecipeInstallation,
    RecipeRun,
)
from ..profile_stop_authority import (
    ProfileStopAuthorityError,
    ProfileStopOwnerBinding,
    validate_profile_stop_owner,
)
from ..recipe_action_plans import (
    StopPlan,
)
from ..recipe_routes import (
    route_publication_transaction,
)
from ..stored_json import read_row_column
from .errors import RecipeRequestInvalid, RecipeStopAuthorityRefused
from .interfaces import new_recipe_job

if TYPE_CHECKING:
    from .service import RecipeOperationService


class StopAcceptanceMixin:
    def _service_profile_stop_owner(
        self,
        session: Session,
        *,
        run: RecipeRun,
        admitted: StopPlan,
        profile_application_id: str,
        request_id: str,
        ordinal: int | None,
    ) -> ProfileStopOwnerBinding:
        service = typing_cast("RecipeOperationService", self)
        from ..fleet_profiles import _persisted_profile_progress
        from ..run_switch_contract import RunSwitchOperationResult
        from ..run_switch_operations import _phase_request_key, _stop_child_request_key

        application = session.get(FleetProfileApplication, profile_application_id)
        if application is None or application.current_step is None or ordinal is None:
            raise RecipeStopAuthorityRefused(
                "accepted profile Stop owner is unavailable"
            )
        adapter = _persisted_profile_progress(application).switch_adapter
        children = (
            [
                child
                for child in adapter.pending_children
                if child.kind in {"stop", "run", "install"}
            ]
            if adapter is not None
            else []
        )
        parents: list[Job] = []
        for child in children:
            candidate = session.get(Job, child.operation_id)
            if candidate is None:
                continue
            expected_kind = (
                "recipe.stop.v2" if child.kind == "stop" else "recipe.run-switch.v2"
            )
            if (
                candidate.kind != expected_kind
                or candidate.payload_digest
                != hashlib.sha256(
                    canonical_message(read_row_column(candidate, "payload"))
                ).hexdigest()
            ):
                raise RecipeStopAuthorityRefused(
                    "accepted profile Stop parent binding changed"
                )
            try:
                candidate_root = RunSwitchJobPayload.model_validate_json(
                    canonical_message(read_row_column(candidate, "payload")),
                    strict=True,
                )
                candidate_progress = RunSwitchOperationResult.model_validate_json(
                    canonical_message(read_row_column(candidate, "result")), strict=True
                )
            except (TypeError, ValueError) as error:
                raise RecipeStopAuthorityRefused(
                    "accepted profile Stop authority is unreadable"
                ) from error
            if (
                candidate_root.operation_kind != candidate.kind
                or candidate_root.action != candidate_root.plan.action
                or (
                    candidate_root.action != "stop"
                    if child.kind == "stop"
                    else candidate_root.action != "install"
                    if child.kind == "install"
                    else candidate_root.action not in {"run", "switch"}
                )
            ):
                raise RecipeStopAuthorityRefused(
                    "accepted profile Stop child action changed"
                )
            phase_index = candidate_progress.phase_index
            item_index = candidate_progress.item_index
            if (
                phase_index is None
                or item_index is None
                or phase_index >= len(candidate_root.plan.phases)
                or item_index >= len(candidate_root.plan.stops)
            ):
                continue
            if (
                candidate_root.plan.phases[phase_index].kind == "stop"
                and candidate_root.plan.stops[item_index].run_id == run.id
            ):
                parents.append(candidate)
        if len(parents) != 1:
            raise RecipeStopAuthorityRefused("accepted profile Stop child is ambiguous")
        parent = session.get(
            Job, parents[0].id, with_for_update=True, populate_existing=True
        )
        if (
            parent is None
            or parent.payload_digest
            != hashlib.sha256(
                canonical_message(read_row_column(parent, "payload"))
            ).hexdigest()
        ):
            raise RecipeStopAuthorityRefused(
                "accepted profile Stop parent binding changed"
            )
        try:
            root = RunSwitchJobPayload.model_validate_json(
                canonical_message(read_row_column(parent, "payload")), strict=True
            )
            # Immutable payload progress is the acceptance snapshot. The
            # current phase and retry identity belong to the canonical result.
            progress = RunSwitchOperationResult.model_validate_json(
                canonical_message(read_row_column(parent, "result")), strict=True
            )
            phase_index = progress.phase_index
            item_index = progress.item_index
            if (
                progress.profile_application_id != application.id
                or progress.workload_intent_ordinal != ordinal
                or progress.cancellation is not None
                or phase_index is None
                or item_index is None
                or phase_index >= len(root.plan.phases)
                or item_index >= len(root.plan.stops)
            ):
                raise RecipeStopAuthorityRefused(
                    "accepted profile Stop phase is unavailable"
                )
            phase = root.plan.phases[phase_index]
            impact = root.plan.stops[item_index]
            generation = progress.phase_retry_generation or 0
            phase_key = (
                _phase_request_key(
                    parent.request_id, phase_index, item_index, generation
                )
                if generation
                else parent.request_id
            )
            if (
                phase.kind != "stop"
                or progress.phase != phase.kind
                or progress.subphase != phase.subphase
                or impact.run_id != run.id
                or request_id
                != _stop_child_request_key(phase_key, run.id, application.id)
            ):
                raise RecipeStopAuthorityRefused(
                    "accepted profile Stop request does not name its exact phase"
                )
            installation = session.get(RecipeInstallation, run.installation_id)
            if installation is None:
                raise RecipeStopAuthorityRefused(
                    "accepted profile Stop installation disappeared"
                )
            owner = ProfileStopOwnerBinding(
                schema_version=1,
                profile_application_id=application.id,
                profile_operation_id=parent.id,
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
                stop_plan_digest=impact.plan_digest,
                workload_intent_ordinal=ordinal,
                run_node_ids=[node.node_id for node in admitted.nodes],
                reachable_node_ids=list(admitted.target_node_ids),
                missing_node_ids=list(admitted.missing_node_ids),
            )
            validate_profile_stop_owner(session, owner, now=service._clock())
        except ProfileStopAuthorityError as error:
            raise RecipeStopAuthorityRefused(
                f"accepted profile Stop authority changed: {redact_text(str(error))}"
            ) from error
        except (TypeError, ValueError) as error:
            raise RecipeStopAuthorityRefused(
                "accepted profile Stop authority is unreadable"
            ) from error
        return owner

    def _accept_service_stop(
        self,
        run_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None,
        profile_target_node_ids: Sequence[str] | None,
        profile_application_id: str | None,
    ) -> Job:
        service = typing_cast("RecipeOperationService", self)
        transaction = (
            service._route_publications.publication_transaction()
            if service._route_publications is not None
            else route_publication_transaction(service._sessions)
        )
        with transaction as session:
            acquire_admission_keys(
                session, (job_request_key(request_id),), holder="recipe-stop-admission"
            )
            existing = service._idempotent_job_in_session(
                session,
                request_id,
                WireAgentOperation.RECIPE_STOP.value,
                plan_digest,
                owner_kind="run",
                owner_id=run_id,
            )
            if existing is not None:
                service._check_service_stop(session, existing)
                return existing
            admitted = service._stop_plan_in_session(
                session,
                run_id,
                lock=True,
                profile_target_node_ids=profile_target_node_ids,
            )
            # This check precedes any ordinal, accepted Job or route effect.
            if not admitted.allowed or admitted.plan_digest != plan_digest:
                raise RecipeRequestInvalid(
                    "stop plan is stale or blocked",
                    reason=InvalidRequestReason.SUPERSEDED,
                )
            run = session.get(RecipeRun, run_id)
            assert run is not None
            if (
                service._absent_stop_nodes(
                    session, run, admitted, service._clock(), lock=True
                )
                is None
            ):
                order, payloads, _targets = service._exact_stop_authority(
                    session, run, admitted
                )
            else:
                order, payloads = None, None
            profile_owner = (
                service._service_profile_stop_owner(
                    session,
                    run=run,
                    admitted=admitted,
                    profile_application_id=profile_application_id,
                    request_id=request_id,
                    ordinal=workload_intent_ordinal,
                )
                if profile_application_id is not None
                else None
            )
            ordinal = service._admit_workload_intent(
                session,
                kind=WireAgentOperation.RECIPE_STOP.value,
                targets=admitted.target_node_ids,
                workload_intent_ordinal=workload_intent_ordinal,
                now=service._clock(),
            )
            document = RecipeStopParent(
                schema_version=1,
                owner_kind="run",
                owner_id=run_id,
                plan_digest=plan_digest,
                workload_intent_ordinal=ordinal,
                service_stop_review=ServiceRunStopReview(
                    stage="accepted",
                    route_state=RouteState(run.route_state),
                    run_generation=run.run_generation,
                    target_node_ids=list(admitted.target_node_ids),
                    missing_node_ids=list(admitted.missing_node_ids),
                    profile_target_node_ids=sorted(profile_target_node_ids)
                    if profile_target_node_ids is not None
                    else None,
                    profile_application_id=profile_application_id,
                    profile_stop_owner=profile_owner,
                    exact_payloads=dict(payloads) if payloads is not None else None,
                    stop_order=list(order) if order is not None else None,
                ),
            )
            now = service._clock()
            job = new_recipe_job(
                id=str(uuid.uuid4()),
                request_id=request_id,
                kind=WireAgentOperation.RECIPE_STOP.value,
                state=LifecycleState.RUNNING.value,
                actor=actor,
                authority_revision=admitted.authority_digest,
                targets=list(admitted.target_node_ids),
                payload=controller_recipe_document(document),
                payload_digest=hashlib.sha256(canonical_message(document)).hexdigest(),
                status_reason="accepted exact Stop; route withdrawal pending",
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()
            return job
