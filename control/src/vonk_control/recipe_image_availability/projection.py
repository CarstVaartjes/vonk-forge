"""Projection for exact recipe image availability."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from vonk_agent_protocol import (
    InvalidRequestReason,
    RecipeImageCode,
    WaitReason,
    canonical_message,
)

from ..categorized_errors import InvalidValue, MissingRecord
from ..job_documents import (
    AvailabilityJobPayload,
    AvailabilityRetry,
)
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..models import (
    Job,
)
from ..recipe_availability_intent import (
    RecipeAvailabilityIntent,
    RecipeRetryIntent,
)
from ..recipe_image_availability_view_contract import (
    RecipeCacheRemovalStatus,
    RecipeImageAvailabilityView,
)
from ..recipe_image_removal_contract import (
    RecipeRemovalProjectionIssue,
    RecipeRemovalUnavailableView,
)
from ..recipe_update_contract import UPDATE_KIND, RecipeUpdateResponse
from ..stored_json import Residue
from ..strict_json import serialize_json_value
from .contracts import (
    OPERATION_KIND,
    REMOVE_OPERATION_KIND,
    RecipeImageAvailabilityInvalid,
    RecipeImageAvailabilityUnknown,
    _iso,
)

if TYPE_CHECKING:
    from .service import RecipeImageAvailabilityService


def _matching_request(
    self: RecipeImageAvailabilityService,
    existing: Job,
    *,
    actor: str,
    intent: RecipeAvailabilityIntent,
) -> RecipeImageAvailabilityView:
    if existing.kind != OPERATION_KIND or existing.actor != actor:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.REQUEST_KEY_REUSED,
            "request key was already used for another operation",
            reason=InvalidRequestReason.CONFLICT,
        )
    try:
        payload = self._payload(existing)
        stored = (
            payload.request if isinstance(payload, AvailabilityJobPayload) else None
        )
    except (TypeError, ValueError, ValidationError) as error:
        # Unreadable stored intent is an unknown observation, not evidence
        # that the caller reused a key for another operation.
        retire_as_unknown(
            "recipe-image.request",
            existing.id,
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            f"{type(error).__name__}: {error}",
        )
        stored = None
    if stored is None:
        raise RecipeImageAvailabilityUnknown(
            RecipeImageCode.OPERATION_INVALID,
            "accepted request evidence is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    if stored != intent:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.REQUEST_KEY_REUSED,
            "request key was already used for another operation",
            reason=InvalidRequestReason.CONFLICT,
        )
    return self._view(existing)


def _request_replay(
    self: RecipeImageAvailabilityService,
    request_id: str,
    *,
    actor: str,
    intent: RecipeAvailabilityIntent,
) -> RecipeImageAvailabilityView | None:
    with self._sessions() as session:
        existing = session.scalar(select(Job).where(Job.request_id == request_id))
        if existing is None:
            return None
        return self._matching_request(existing, actor=actor, intent=intent)


def get(
    self: RecipeImageAvailabilityService, operation_id: str
) -> RecipeImageAvailabilityView:
    with self._sessions() as session:
        operation = session.get(Job, operation_id)
        if operation is None or operation.kind != OPERATION_KIND:
            raise MissingRecord(operation_id)
        return self._view(operation)


def get_operator_operation(
    self: RecipeImageAvailabilityService, operation_id: str
) -> (
    RecipeImageAvailabilityView
    | RecipeUpdateResponse
    | RecipeRemovalUnavailableView
    | RecipeCacheRemovalStatus
):
    """Observe either current recipe preparation or durable cache removal."""

    with self._sessions() as session:
        operation = session.get(Job, operation_id)
        if operation is None:
            raise MissingRecord(operation_id)
        if operation.kind == OPERATION_KIND:
            return self._view(operation)
        if operation.kind == REMOVE_OPERATION_KIND:
            try:
                intent = self._read_removal_intent(operation)
                return self._read_removal_result(operation, intent)
            except RecipeImageAvailabilityUnknown:
                return RecipeRemovalUnavailableView(
                    operation_id=operation.id,
                    request_key=operation.request_id,
                    recipe_revision_id=operation.authority_revision,
                    observed_at=_iso(operation.updated_at),
                    projection_issue=RecipeRemovalProjectionIssue(
                        detail="The accepted recipe removal record cannot be read. Its effects and outcome are unknown.",
                        next_action="The worker retires unreadable removal intent and reconciles its storage fences automatically. Recheck for the observed outcome.",
                    ),
                )
        if operation.kind != UPDATE_KIND:
            raise MissingRecord(operation_id)
    return self._updates.get(operation_id)


def get_operator_request(
    self: RecipeImageAvailabilityService, request_key: str, *, actor: str
) -> (
    RecipeImageAvailabilityView
    | RecipeUpdateResponse
    | RecipeRemovalUnavailableView
    | RecipeCacheRemovalStatus
):
    """Correlate only this issuer's request within the recipe family."""

    with self._sessions() as session:
        operation = session.scalar(
            select(Job).where(Job.request_id == request_key, Job.actor == actor)
        )
        if operation is None or operation.kind not in {
            OPERATION_KIND,
            REMOVE_OPERATION_KIND,
            UPDATE_KIND,
        }:
            raise MissingRecord(request_key)
        operation_id = operation.id
    return self.get_operator_operation(operation_id)


def list_page(
    self: RecipeImageAvailabilityService,
    *,
    recipe_revision_id: str | None = None,
    state: str | None = None,
    limit: int = 50,
    boundary: tuple[str, str] | None = None,
) -> tuple[tuple[RecipeImageAvailabilityView, ...], int, tuple[str, str] | None]:
    if not 1 <= limit <= 100:
        raise InvalidValue(
            "availability list limit is invalid",
            reason=InvalidRequestReason.OUT_OF_RANGE,
        )
    with self._sessions() as session:
        query = select(Job).where(Job.kind == OPERATION_KIND)
        count_query = (
            select(func.count()).select_from(Job).where(Job.kind == OPERATION_KIND)
        )
        if recipe_revision_id is not None:
            query = query.where(Job.authority_revision == recipe_revision_id)
            count_query = count_query.where(
                Job.authority_revision == recipe_revision_id
            )
        if state is not None:
            query = query.where(Job.state == state)
            count_query = count_query.where(Job.state == state)
        if boundary is not None:
            boundary_time = datetime.fromisoformat(boundary[0])
            query = query.where(
                or_(
                    Job.created_at < boundary_time,
                    (Job.created_at == boundary_time) & (Job.id < boundary[1]),
                )
            )
        total = int(session.scalar(count_query) or 0)
        rows = tuple(
            session.scalars(
                query.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit + 1)
            )
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
    next_boundary = None
    if has_more and rows:
        last = rows[-1]
        next_boundary = (_iso(last.created_at), last.id)
    return tuple(self._view(row) for row in rows), total, next_boundary


def retry(
    self: RecipeImageAvailabilityService,
    operation_id: str,
    *,
    actor: str,
    request_id: str,
) -> RecipeImageAvailabilityView:
    intent = RecipeRetryIntent(operation_id=operation_id)
    existing = self._request_replay(request_id, actor=actor, intent=intent)
    if existing is not None:
        return existing
    with self._sessions() as session:
        previous = session.get(Job, operation_id)
        if previous is None or previous.kind != OPERATION_KIND:
            raise MissingRecord(operation_id)
        if previous.state != "failed":
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.NOT_RETRYABLE, "operation is not failed"
            )
        previous_payload = self._payload(previous)
        if isinstance(previous_payload, Residue):
            return self._unknown_view(previous, previous_payload)
        retry_count = previous_payload.retry.operator_retries
        previous_authority = previous.authority_revision
        previous_targets = list(previous.targets)
        payload = previous_payload
    payload = payload.model_copy(update={"request": intent})
    with self._sessions.begin() as session:
        existing = session.scalar(select(Job).where(Job.request_id == request_id))
        if existing is not None:
            return self._matching_request(existing, actor=actor, intent=intent)
        payload = payload.model_copy(
            update={
                "retry": AvailabilityRetry(
                    automatic_attempts=0, operator_retries=retry_count + 1
                )
            }
        )
        payload = payload.model_copy(update={"retry_after_at": None})
        payload = payload.model_copy(update={"failure": None})
        payload = payload.model_copy(update={"build_dependency": None})
        now = self._clock()
        encoded = canonical_message(payload)
        operation = self._lifecycle.new_job(
            id=str(uuid.uuid4()),
            request_id=request_id,
            kind=OPERATION_KIND,
            actor=actor,
            authority_revision=previous_authority,
            targets=previous_targets,
            payload_digest=hashlib.sha256(encoded).hexdigest(),
            payload=serialize_json_value(payload),
            result=None,
            current_attempt=0,
            created_at=now,
            updated_at=now,
        )
        session.add(operation)
        session.flush()
        return self._view(operation)
