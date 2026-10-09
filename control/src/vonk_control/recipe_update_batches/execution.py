"""Recipe update batches: execution."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    ProgressPhase,
    RecipeUpdateCode,
    WaitReason,
    canonical_message,
)

from ..categorized_errors import InvalidValue
from ..lifecycle import State
from ..lifecycle.recipe_update_batch import CANCEL_BUDGET
from ..logging import redact_text
from ..models import CatalogDocumentRevision, Job
from ..recipe_availability_intent import RecipeRevisionIntent
from ..recipe_image_availability import (
    RecipeImageAvailabilityRefused,
    RecipeImageAvailabilityUnknown,
)
from ..recipe_update_contract import (
    UPDATE_KIND,
    RecipeUpdateChild,
    RecipeUpdateDocument,
    RecipeUpdateFailure,
    UpdateChildState,
    read_update_document,
)
from ..strict_json import serialize_json_value

if TYPE_CHECKING:
    from .service import RecipeUpdateBatches

from .helpers import (
    _OBSERVATION_INTERVAL,
    _SETTLED,
    RecipeUpdateClaim,
    RecipeUpdateClaimLost,
    _now,
)


class ExecutionMixin:
    def claim(self, owner: str) -> RecipeUpdateClaim | None:
        service = cast("RecipeUpdateBatches", self)
        from ..recipe_image_availability import RecipeImageAvailabilityError

        if not owner or len(owner) > 95:
            raise InvalidValue("update worker owner must contain 1 to 95 characters")
        now = _now(service.owner._clock())
        with service.sessions() as session:
            candidates = list(
                session.scalars(
                    select(Job.id)
                    .where(
                        Job.kind == UPDATE_KIND, Job.state.in_(("queued", "running"))
                    )
                    .order_by(Job.updated_at, Job.id)
                )
            )
        for operation_id in candidates:
            with service.sessions.begin() as session:
                job = session.scalar(
                    select(Job)
                    .where(Job.id == operation_id, Job.state.in_(("queued", "running")))
                    .with_for_update(skip_locked=True)
                )
                if job is None:
                    continue
                try:
                    document = service._document(job)
                except RecipeImageAvailabilityError as error:
                    # The document is the evidence of what was admitted: retained
                    # as it is, the batch ends with the reason and is not advanced.
                    service._lifecycle.reject(job, error.code, now)
                    job.result = {
                        "code": error.code,
                        "detail": error.detail,
                        "retryable": False,
                    }
                    continue
                if document.claim_until is not None and document.claim_until > now:
                    continue
                if (
                    document.next_attempt_at is not None
                    and document.next_attempt_at > now
                ):
                    continue
                claim_owner = f"{owner}:{uuid.uuid4().hex}"
                claim_until = now + timedelta(
                    seconds=service.owner._claim_lease_seconds
                )
                claimed = service._lifecycle.claimed(
                    job, document, claim_owner, claim_until, now
                )
                if claimed.state is not State.RUNNING:
                    continue  # a cancel or another claim won: the core refused
                document.claim_owner = claim_owner
                document.claim_until = claim_until
                job.payload = serialize_json_value(document)
                return RecipeUpdateClaim(job.id, document.claim_owner)
        return None

    def _owned(
        self, session: Session, claim: RecipeUpdateClaim
    ) -> tuple[Job, RecipeUpdateDocument]:
        service = cast("RecipeUpdateBatches", self)
        job = session.scalar(
            select(Job)
            .where(Job.id == claim.operation_id, Job.kind == UPDATE_KIND)
            .with_for_update(nowait=True)
        )
        if job is None:
            raise RecipeUpdateClaimLost(
                RecipeUpdateCode.CLAIM_LOST,
                "recipe update no longer owns its claim",
                reason=WaitReason.LEASE_LAPSED,
                retryable=False,
            )
        document = service._document(job)
        if (
            job.state != "running"
            or document.claim_owner != claim.owner
            or document.claim_until is None
            or document.claim_until <= _now(service.owner._clock())
        ):
            raise RecipeUpdateClaimLost(
                RecipeUpdateCode.CLAIM_LOST,
                "recipe update no longer owns its claim",
                reason=WaitReason.LEASE_LAPSED,
                retryable=False,
            )
        return job, document

    def authorize_child(
        self,
        session: Session,
        claim: RecipeUpdateClaim,
        actor: str,
        request_id: str,
        intent: RecipeRevisionIntent,
    ) -> None:
        # The batch was authorized at acceptance. Platform maintenance keeps
        # its exact children moving even if the original author is removed.
        # Common order: parent Job, child request's unique key.
        # The caller's accepting transaction contains no metadata/storage I/O.
        service = cast("RecipeUpdateBatches", self)
        job, document = service._owned(session, claim)
        child = next(
            (item for item in document.children if item.request_key == request_id), None
        )
        if job.actor != actor or child is None or intent != service._intent(child):
            raise RecipeImageAvailabilityUnknown(
                RecipeUpdateCode.OBSERVATION_INVALID,
                "accepted child scope observation is unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        revision = session.get(CatalogDocumentRevision, child.recipe_revision_id)
        if (
            revision is None
            or revision.content_digest != child.recipe_content_sha256
            or revision.execution_key != child.effective_execution_key
        ):
            raise RecipeImageAvailabilityUnknown(
                RecipeUpdateCode.OBSERVATION_INVALID,
                "accepted child recipe evidence is unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )

    @staticmethod
    def _intent(child: RecipeUpdateChild) -> RecipeRevisionIntent:
        return RecipeRevisionIntent(
            recipe_revision_id=child.recipe_revision_id,
            effective_execution_key=child.effective_execution_key,
            force=True,
        )

    def run(self, claim: RecipeUpdateClaim) -> None:
        service = cast("RecipeUpdateBatches", self)
        try:
            service._run_once(claim)
        except RecipeUpdateClaimLost:
            # An expired/replaced/deleted parent is not work for this executor
            # to retry. Its current owner keeps every committed child identity;
            # this stale invocation exits without overwriting their checkpoint.
            return

    def _run_once(self, claim: RecipeUpdateClaim) -> None:
        service = cast("RecipeUpdateBatches", self)
        from ..recipe_image_availability import (
            RecipeImageAvailabilityError,
            RecipeImageAvailabilityView,
        )

        with service.sessions.begin() as session:
            job, document = service._owned(session, claim)
            actor = job.actor
            observation_deadline = _now(job.created_at) + CANCEL_BUDGET
        now = _now(service.owner._clock())
        eligible = [
            index
            for index, child in enumerate(document.children)
            if child.state not in _SETTLED
            and (child.retry_at is None or child.retry_at <= now)
        ]
        if eligible:
            index = next(
                (item for item in eligible if item >= document.next_child), eligible[0]
            )
            child = document.children[index]
            try:
                try:
                    observed = service.owner.get_operator_request(
                        child.request_key, actor=actor
                    )
                except KeyError:
                    observed = service.owner._start_request(
                        service._intent(child),
                        actor=actor,
                        request_id=child.request_key,
                        update_claim=claim,
                    )
                # Validate the whole peer response before reading any field.
                # Even a model instance may have been constructed unchecked.
                if not isinstance(observed, RecipeImageAvailabilityView):
                    raise TypeError("child observation is unreadable")
                observed = RecipeImageAvailabilityView.model_validate_json(
                    canonical_message(observed.model_dump(mode="json", by_alias=True)),
                    strict=True,
                )
                if (
                    observed.request != service._intent(child)
                    or observed.recipe_content_sha256 != child.recipe_content_sha256
                ):
                    raise RecipeImageAvailabilityUnknown(
                        RecipeUpdateCode.OBSERVATION_INVALID,
                        "child receipt does not establish the accepted content and request",
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                failure = observed.failure_evidence
                candidate = child.model_copy(
                    update={
                        "operation_id": observed.id,
                        "state": observed.state,
                        "failure": None
                        if failure is None
                        else RecipeUpdateFailure(
                            code=failure.code,
                            detail=str(redact_text(failure.detail))[:512],
                            retryable=failure.retryable,
                        ),
                        "retry_at": None,
                    }
                )
                child = RecipeUpdateChild.model_validate_json(
                    canonical_message(candidate.model_dump(mode="json")), strict=True
                )
                document.children[index] = child
            except RecipeUpdateClaimLost:
                raise
            except (
                RecipeImageAvailabilityUnknown,
                RecipeImageAvailabilityError,
            ) as error:
                # Accepted-scope peer errors are observations unless the peer
                # reports a refusal at its actual authority/ingress boundary.
                # A non-retryable bookkeeping projection is still re-read.
                retryable = (
                    not isinstance(error, RecipeImageAvailabilityRefused)
                    and now < observation_deadline
                )
                child.failure = RecipeUpdateFailure(
                    code=error.code,
                    detail=str(redact_text(error.detail))[:512],
                    retryable=retryable,
                )
                child.state = (
                    cast(UpdateChildState, ProgressPhase.PENDING.value)
                    if retryable
                    else LifecycleState.FAILED
                )
                child.retry_at = (
                    min(
                        observation_deadline,
                        now
                        + timedelta(
                            seconds=min(300, max(2, error.retry_after_seconds or 2))
                        ),
                    )
                    if retryable
                    else None
                )
            except (ValueError, TypeError, AttributeError):
                # Observation never becomes a failed security decision. This
                # observer has the same bounded reconciliation window as cleanup;
                # issued children retain their own executor fences and lifecycle.
                retryable = now < observation_deadline
                child.state = (
                    cast(UpdateChildState, ProgressPhase.PENDING.value)
                    if retryable
                    else LifecycleState.FAILED
                )
                child.failure = RecipeUpdateFailure(
                    code=RecipeUpdateCode.OBSERVATION_INVALID,
                    detail="child operation evidence is unavailable",
                    retryable=retryable,
                )
                child.retry_at = (
                    min(observation_deadline, now + _OBSERVATION_INTERVAL)
                    if retryable
                    else None
                )
            child.observed_at = now
            document.next_child = (index + 1) % len(document.children)
        with service.sessions.begin() as session:
            job, _ = service._owned(session, claim)
            run_now = _now(service.owner._clock())
            # The claim ends with this pass; the batch then follows its children.
            document.claim_owner = document.claim_until = None
            if all(child.state in _SETTLED for child in document.children):
                document.next_attempt_at = None
                service._lifecycle.conclude(job, document, run_now)
            else:
                document.next_attempt_at = min(
                    child.retry_at
                    or (
                        now if child.state == "pending" else now + _OBSERVATION_INTERVAL
                    )
                    for child in document.children
                    if child.state not in _SETTLED
                )
                service._lifecycle.project(
                    job,
                    document,
                    run_now,
                    visible=(
                        "running"
                        if any(
                            child.operation_id is not None
                            for child in document.children
                        )
                        else "queued"
                    ),
                )
            # Validate the persisted current contract, including JSON-mode fields.
            job.payload = serialize_json_value(
                read_update_document(serialize_json_value(document))
            )
            job.updated_at = _now(service.owner._clock())
