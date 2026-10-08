"""Stop for digest-bound recipe operations."""

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
    RouteState,
    UnknownOutcomeError,
    canonical_message,
)

from ..admission_locking import (
    admission_attempts,
    admission_wait_exhausted,
)
from ..job_documents import (
    RecipeStopParent,
)
from ..logging import redact_text
from ..models import (
    AgentOperation,
    Job,
    RecipeRun,
)
from ..profile_stop_authority import (
    ProfileJobRunStopJob,
)
from ..recipe_action_plans import (
    StopPlan,
)
from ..recipe_routes import (
    RecipeRouteError,
    RecipeRouteNotReady,
    publication_is_temporary,
    route_publication_transaction,
)
from ..stored_json import read_row_column
from ..strict_json import serialize_json_value
from .constants import _STOP_WITHDRAWAL_ATTEMPTS
from .errors import (
    RecipeArtifactJobCancellationPending,
    RecipeRequestInvalid,
    RecipeRetryLater,
    RecipeStopAuthorityRefused,
    _RouteNotWithdrawn,
    _ServiceStopReplay,
)
from .intent import _bound_workload_intent
from .interfaces import RecipeOperationView

if TYPE_CHECKING:
    from .service import RecipeOperationService


class StopMixin:
    def preview_stop(
        self,
        run_id: str,
        *,
        profile_target_node_ids: Sequence[str] | None = None,
    ) -> StopPlan:
        service = typing_cast("RecipeOperationService", self)
        with service._sessions() as session:
            return service._stop_plan_in_session(
                session,
                run_id,
                lock=False,
                profile_target_node_ids=profile_target_node_ids,
            )

    def stop(
        self,
        run_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
        profile_target_node_ids: Sequence[str] | None = None,
        profile_application_id: str | None = None,
    ) -> RecipeOperationView:
        service = typing_cast("RecipeOperationService", self)
        existing = service._idempotent(
            request_id,
            WireAgentOperation.RECIPE_STOP.value,
            None,
            owner_kind="run",
            owner_id=run_id,
        )
        pending_service = (
            service._service_stop_pending(
                request_id, plan_digest, profile_target_node_ids, profile_application_id
            )
            if existing is not None
            else False
        )
        if (
            existing is not None
            and not pending_service
            and not (
                existing.state == LifecycleState.RUNNING.value
                and (
                    service._one_shot_stop_is_pending(request_id)
                    or service._profile_jobrun_stop_is_pending(request_id)
                )
            )
        ):
            return existing
        refused: UnknownOutcomeError | None = None
        logical: RecipeOperationView | RecipeArtifactJobCancellationPending | None = (
            None
        )
        for _attempt in admission_attempts():
            try:
                logical = service._stop_logical_job_run(
                    run_id,
                    plan_digest=plan_digest,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                    profile_target_node_ids=profile_target_node_ids,
                    profile_application_id=profile_application_id,
                )
                refused = None
                break
            except UnknownOutcomeError as error:
                refused = error
                if admission_wait_exhausted(error):
                    break
        if refused is not None:
            raise refused
        if isinstance(logical, RecipeArtifactJobCancellationPending):
            raise logical
        if logical is not None:
            if logical.state == LifecycleState.RUNNING.value:
                service._agent_jobs.notify_available()
            return logical
        for _attempt in admission_attempts():
            try:
                return service._stop_service_run_once(
                    run_id,
                    plan_digest=plan_digest,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                    profile_target_node_ids=profile_target_node_ids,
                    profile_application_id=profile_application_id,
                )
            except _ServiceStopReplay as replay:
                return replay.operation
            except UnknownOutcomeError as error:
                refused = error
                if admission_wait_exhausted(error):
                    break
        assert refused is not None
        raise refused

    def _stop_service_run_once(
        self,
        run_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None,
        profile_target_node_ids: Sequence[str] | None,
        profile_application_id: str | None,
    ) -> RecipeOperationView:
        # Claim, effect, conditional completion. The accepted Stop is checked
        # durably first so a stale or blocked request withdraws nothing; the
        # route is then withdrawn with no transaction open (the withdrawal
        # intent is durable with its claim, and a crash resumes from it); only
        # then is the Stop dispatched, in its own short transaction, and only
        # while the withdrawal is still complete.
        service = typing_cast("RecipeOperationService", self)
        accepted = service._accept_service_stop(
            run_id,
            plan_digest=plan_digest,
            actor=actor,
            request_id=request_id,
            workload_intent_ordinal=workload_intent_ordinal,
            profile_target_node_ids=profile_target_node_ids,
            profile_application_id=profile_application_id,
        )
        workload_intent_ordinal = _bound_workload_intent(accepted)
        assert type(workload_intent_ordinal) is int

        def claim_withdrawal(session: Session) -> None:
            parent = session.get(Job, accepted.id, with_for_update=True)
            if parent is None:
                raise RecipeStopAuthorityRefused("accepted Stop disappeared")
            document, _admitted = service._check_service_stop(session, parent)
            review = document.service_stop_review
            assert review is not None
            document.service_stop_review = review.model_copy(
                update={"stage": "withdrawal-claimed"}
            )
            service._write_stop_parent(parent, document, now=service._clock())

        for _attempt in range(_STOP_WITHDRAWAL_ATTEMPTS):
            if service._route_publications is not None:
                try:
                    service._route_publications.withdraw_run(
                        run_id, pending="stop", before_withdrawal=claim_withdrawal
                    )
                except RecipeRouteNotReady as error:
                    raise RecipeRequestInvalid(
                        "the run's route withdrawal was superseded; retry the stop",
                        reason=InvalidRequestReason.SUPERSEDED,
                    ) from error
                except PermissionError:
                    raise
                except RecipeRouteError as error:
                    if isinstance(
                        error.__cause__, PermissionError
                    ) or not publication_is_temporary(error):
                        raise
                    return service._defer_accepted_service_stop(accepted.id, error)
                except (OSError, UnknownOutcomeError) as error:
                    return service._defer_accepted_service_stop(accepted.id, error)
            else:
                with route_publication_transaction(service._sessions) as session:
                    claim_withdrawal(session)
                try:
                    service._route_withdrawer(run_id)
                except PermissionError:
                    raise
                except (OSError, UnknownOutcomeError) as error:
                    return service._defer_accepted_service_stop(accepted.id, error)
            try:
                job = service._dispatch_stop_after_withdrawal(
                    run_id,
                    plan_digest=plan_digest,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                    profile_target_node_ids=profile_target_node_ids,
                )
            except _RouteNotWithdrawn:
                continue
            except UnknownOutcomeError as error:
                return service._defer_accepted_service_stop(accepted.id, error)
            if job.state != LifecycleState.SUCCEEDED.value:
                service._agent_jobs.notify_available()
            return job
        return service._defer_accepted_service_stop(
            accepted.id,
            RecipeRetryLater("the run's route withdrawal has not settled"),
        )

    def _defer_accepted_service_stop(
        self, parent_id: str, error: OSError | UnknownOutcomeError | RecipeRouteError
    ) -> RecipeOperationView:
        # The response was lost after consent was committed. Returning the
        # SAME running child lets RunSwitch retain its phase/request identity;
        # the normal bounded Stop continuation owns the external retry.
        service = typing_cast("RecipeOperationService", self)
        now = service._clock()
        with service._sessions.begin() as session:
            parent = session.get(Job, parent_id, with_for_update=True)
            if parent is None:
                raise RecipeStopAuthorityRefused("accepted Stop disappeared")
            service._check_service_stop(session, parent)
            parent.status_reason = (
                f"accepted exact Stop deferred: {redact_text(str(error))}; next reconciliation at {(now + timedelta(seconds=5)).isoformat()}"
            )[:1024]
            parent.updated_at = now
            session.flush()
            return service._view(parent, session=session)

    @staticmethod
    def _service_stop_document(job: Job) -> RecipeStopParent:
        if (
            job.kind != WireAgentOperation.RECIPE_STOP.value
            or hashlib.sha256(
                canonical_message(read_row_column(job, "payload"))
            ).hexdigest()
            != job.payload_digest
        ):
            raise RecipeStopAuthorityRefused(
                "accepted Stop document does not match its immutable binding"
            )
        try:
            document = RecipeStopParent.model_validate_json(
                canonical_message(read_row_column(job, "payload"))
            )
        except (TypeError, ValueError) as error:
            raise RecipeStopAuthorityRefused(
                "accepted Stop document is unreadable"
            ) from error
        if document.owner_kind != "run":
            raise RecipeStopAuthorityRefused("accepted Stop owner changed")
        return document

    @staticmethod
    def _write_stop_parent(
        job: Job, document: RecipeStopParent | ProfileJobRunStopJob, *, now: datetime
    ) -> None:
        job.payload = serialize_json_value(document)
        job.payload_digest = hashlib.sha256(
            canonical_message(read_row_column(job, "payload"))
        ).hexdigest()
        job.updated_at = now

    def _service_stop_pending(
        self,
        request_id: str,
        plan_digest: str,
        profile_target_node_ids: Sequence[str] | None,
        profile_application_id: str | None,
    ) -> bool:
        service = typing_cast("RecipeOperationService", self)
        with service._sessions() as session:
            job = session.scalar(select(Job).where(Job.request_id == request_id))
            assert job is not None
            if (
                getattr(read_row_column(job, "payload"), "service_stop_review", None)
                is None
            ):
                return False
            document = service._service_stop_document(job)
            review = document.service_stop_review
            if review is None:
                # Already-issued and other canonical Stop kinds remain observers;
                # an absent historical review never authorizes new dispatch.
                return False
            if (
                document.plan_digest != plan_digest
                or review.profile_target_node_ids
                != (
                    sorted(profile_target_node_ids)
                    if profile_target_node_ids is not None
                    else None
                )
                or review.profile_application_id != profile_application_id
            ):
                raise RecipeRequestInvalid(
                    "request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
            return (
                job.state == LifecycleState.RUNNING.value
                and review.stage != "dispatched"
            )

    def _check_service_stop(
        self,
        session: Session,
        job: Job,
    ) -> tuple[RecipeStopParent, StopPlan]:
        service = typing_cast("RecipeOperationService", self)
        document = service._service_stop_document(job)
        review = document.service_stop_review
        if (
            review is None
            or review.stage == "dispatched"
            or job.state != LifecycleState.RUNNING.value
            or document.phases is not None
        ):
            # The request lookup and immutable receipt validation precede this
            # check. Another caller (or an earlier admission attempt) may have
            # dispatched since our initial lookup. Observe its receipt without
            # revalidating live withdrawal authority or repeating any effect.
            raise _ServiceStopReplay(service._view(job, session=session))
        if (
            session.scalar(
                select(AgentOperation.id)
                .where(AgentOperation.parent_job_id == job.id)
                .limit(1)
            )
            is not None
        ):
            raise RecipeStopAuthorityRefused("accepted Stop already has issued work")
        run = session.get(RecipeRun, document.owner_id, with_for_update=True)
        if run is None or run.run_generation != review.run_generation:
            raise RecipeStopAuthorityRefused("accepted Stop runtime generation changed")
        if run.route_state != review.route_state.value and not (
            review.stage == "withdrawal-claimed"
            and run.route_state == RouteState.WITHDRAWN
        ):
            raise RecipeRequestInvalid(
                "reviewed Stop route state changed",
                reason=InvalidRequestReason.SUPERSEDED,
            )
        admitted = service._stop_plan_in_session(
            session,
            run.id,
            lock=True,
            profile_target_node_ids=review.profile_target_node_ids,
            reviewed_route_state=review.route_state.value,
        )
        if (
            not admitted.allowed
            or admitted.plan_digest != document.plan_digest
            or admitted.authority_digest != job.authority_revision
            or list(admitted.target_node_ids) != review.target_node_ids
            or list(admitted.missing_node_ids) != review.missing_node_ids
            or sorted(job.targets) != review.target_node_ids
        ):
            raise RecipeRequestInvalid(
                "reviewed Stop effects changed", reason=InvalidRequestReason.SUPERSEDED
            )
        ordinal = _bound_workload_intent(job)
        if review.profile_application_id is not None:
            current_owner = service._service_profile_stop_owner(
                session,
                run=run,
                admitted=admitted,
                profile_application_id=review.profile_application_id,
                request_id=job.request_id,
                ordinal=ordinal,
            )
            if review.profile_stop_owner is None or canonical_message(
                current_owner
            ) != canonical_message(review.profile_stop_owner):
                raise RecipeStopAuthorityRefused("accepted profile Stop owner changed")
        elif review.profile_stop_owner is not None:
            raise RecipeStopAuthorityRefused(
                "accepted Stop profile binding is inconsistent"
            )
        service._admit_workload_intent(
            session,
            kind=WireAgentOperation.RECIPE_STOP.value,
            targets=job.targets,
            workload_intent_ordinal=ordinal,
            now=service._clock(),
        )
        if review.exact_payloads is None:
            if (
                service._absent_stop_nodes(
                    session, run, admitted, service._clock(), lock=True
                )
                is None
            ):
                raise RecipeStopAuthorityRefused(
                    "accepted absence-only Stop no longer has exact absence evidence"
                )
        else:
            current_order, current_payloads, _targets = service._exact_stop_authority(
                session, run, admitted
            )
            if (
                current_order is None
                or list(current_order) != review.stop_order
                or canonical_message(current_payloads)
                != canonical_message(review.exact_payloads)
            ):
                raise RecipeStopAuthorityRefused(
                    "accepted Stop runtime payload authority changed"
                )
        return document, admitted
