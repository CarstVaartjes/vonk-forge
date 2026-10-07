"""Cancellation reconcile for exact recipe image availability."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from vonk_agent_protocol import (
    LifecycleState,
    RuntimeImageCode,
)

from .. import job_states
from ..job_documents import (
    AvailabilityJobPayload,
)
from ..lifecycle.core import STOP_BUDGET
from ..models import (
    Job,
)
from ..runtime_image_preparation import (
    RuntimeImagePreparationError,
)
from ..stored_json import Residue
from ..strict_json import serialize_json_value
from .contracts import (
    OPERATION_KIND,
    RecipeImageAvailabilityClaim,
    _AvailabilityClaimLost,
)

if TYPE_CHECKING:
    from ..recipe_update_batches import RecipeUpdateClaim
    from .service import RecipeImageAvailabilityService


def _release_cancelled_claim(
    self: RecipeImageAvailabilityService, claim: RecipeImageAvailabilityClaim
) -> bool:
    with self._sessions() as session:
        operation = session.get(Job, claim.operation_id)
        payload = self._payload(operation) if operation is not None else None
        if (
            operation is None
            or not isinstance(payload, AvailabilityJobPayload)
            or operation.kind != OPERATION_KIND
            or operation.state not in job_states.words(LifecycleState.OBSERVING)
            or operation.current_attempt != claim.execution_attempt
            or not isinstance(operation.payload, Mapping)
            or payload.claim_owner != claim.claim_owner
            or self._stored_cancellation(operation) is None
            or payload.removal_fence is not None
        ):
            return False
        snapshot = self._payload(operation)
        if isinstance(snapshot, Residue):
            return False
    try:
        reference = self._image_reference_intent_for_claim(snapshot, claim)
    except _AvailabilityClaimLost:
        return False
    lock = (
        self._storage.publication_lock(reference.oci_archive_sha256)
        if reference is not None
        else nullcontext()
    )
    try:
        with lock, self._sessions.begin() as session:
            operation = session.scalar(
                select(Job)
                .where(Job.id == claim.operation_id, Job.kind == OPERATION_KIND)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
            payload = self._payload(operation) if operation is not None else None
            if (
                operation is None
                or not isinstance(payload, AvailabilityJobPayload)
                or operation.state not in job_states.words(LifecycleState.OBSERVING)
                or operation.current_attempt != claim.execution_attempt
                or not isinstance(operation.payload, Mapping)
                or payload.claim_owner != claim.claim_owner
                or payload.removal_fence is not None
                or self._stored_cancellation(operation) is None
            ):
                return False
            payload = self._payload(operation)
            if isinstance(payload, Residue):
                return False
            try:
                latest_reference = self._image_reference_intent_for_claim(
                    payload, claim
                )
            except _AvailabilityClaimLost:
                return False
            if latest_reference != reference:
                # A publisher may have entered its guarded callback
                # after our snapshot. Let the next pass claim its exact
                # archive lock before releasing the owner.
                return False
            payload = payload.model_copy(update={"image_reference_intent": None})
            payload = payload.model_copy(update={"claim_owner": None})
            payload = payload.model_copy(update={"claim_until": None})
            operation.payload = serialize_json_value(payload)
            operation.updated_at = self._clock()
            return True
    except RuntimeImagePreparationError as error:
        if error.code == RuntimeImageCode.PUBLICATION_CONTENDED:
            return False
        raise
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == "55P03":
            return False
        raise


def _reconcile_availability_cancellation(
    self: RecipeImageAvailabilityService, operation_id: str
) -> bool:
    changed = self._reconcile_cancellation_pass(operation_id)
    return self._end_spent_cancellation(operation_id) or changed


def _end_spent_cancellation(
    self: RecipeImageAvailabilityService, operation_id: str
) -> bool:
    """Rule 4: a cancel whose budget is spent ends ``cancelled``, effect unknown.

    Whatever is still outstanding (a lease, a child that does not stop) keeps
    its own lifecycle; the claim is fenced by the ended state.
    """

    now = self._clock()
    try:
        with self._sessions.begin() as session:
            current = session.scalar(
                select(Job)
                .where(Job.id == operation_id, Job.kind == OPERATION_KIND)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
            if current is None or current.state not in job_states.words(
                LifecycleState.OBSERVING
            ):
                return False
            row = self._lifecycle.lifecycle(current, now)
            if row.observe_count < STOP_BUDGET:
                return False
            ended = self._lifecycle.settle_cancel(current, now, outstanding=True)
            return ended.terminal
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == "55P03":
            return False
        raise


def _reconcile_cancellation_pass(
    self: RecipeImageAvailabilityService, operation_id: str
) -> bool:
    with self._sessions() as session:
        operation = session.get(Job, operation_id)
        if (
            operation is None
            or operation.kind != OPERATION_KIND
            or operation.state not in job_states.words(LifecycleState.OBSERVING)
        ):
            return False
        cancellation = self._stored_cancellation(operation)
        payload = self._payload(operation)
        if isinstance(payload, Residue):
            return False
        request_id = operation.request_id
        current_attempt = int(operation.current_attempt)
    if cancellation is None:
        return False
    model_pending, child_changed = self._model_child_cancellation_pending(
        operation_id, payload, request_id, cancellation
    )
    build_pending = self._build_child_cancellation_pending(
        operation_id, payload, current_attempt, cancellation
    )
    now = self._clock()
    now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    owner = payload.claim_owner
    if owner is not None:
        if not isinstance(owner, str):
            return child_changed
        if payload.image_reference_intent is None:
            until = payload.claim_until
            if until is None:
                return child_changed
            if until > now:
                # A valid lease may still be transferring bytes. Give its
                # exact owner the chance to retain a verified receipt under
                # the archive lock; expiry later fences callbacks in SQL.
                return child_changed
        recipe_revision_id = operation.authority_revision
        if not isinstance(recipe_revision_id, str):
            return child_changed
        build_input_sha256 = payload.build_input_sha256
        claim = RecipeImageAvailabilityClaim(
            operation_id=operation_id,
            recipe_revision_id=recipe_revision_id,
            build_input_sha256=(
                build_input_sha256 if isinstance(build_input_sha256, str) else None
            ),
            claim_owner=owner,
            execution_attempt=current_attempt,
        )
        if not self._release_cancelled_claim(claim):
            return child_changed
    elif payload.image_reference_intent is not None:
        # An intent without its exact claim owner is malformed and cannot
        # be treated as a stale, harmless record.
        return child_changed
    if model_pending or build_pending:
        return child_changed
    with self._sessions.begin() as session:
        current = session.scalar(
            select(Job)
            .where(Job.id == operation_id, Job.kind == OPERATION_KIND)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
        if current is None or current.state not in job_states.words(
            LifecycleState.OBSERVING
        ):
            return child_changed
        current_cancellation = self._stored_cancellation(current)
        if (
            current_cancellation is None
            or current_cancellation.cancel_request_id != cancellation.cancel_request_id
        ):
            return child_changed
        current_payload = self._payload(current)
        if isinstance(current_payload, Residue):
            return False
        if (
            current_payload.claim_owner is not None
            or current_payload.image_reference_intent is not None
            or current_payload.removal_fence is not None
        ):
            return child_changed
        self._lifecycle.settle_cancel(current, now, outstanding=False)
        current.status_reason = cancellation.reason
        return True


def claim_update(
    self: RecipeImageAvailabilityService, owner: str
) -> RecipeUpdateClaim | None:
    return self._updates.claim(owner)


def run_update_claim(
    self: RecipeImageAvailabilityService, claim: RecipeUpdateClaim
) -> None:
    self._updates.run(claim)


def update_activity_provider(self: RecipeImageAvailabilityService):
    return self._updates.activity_provider()
