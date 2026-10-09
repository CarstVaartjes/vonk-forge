"""Recipe update batches: cancellation."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
)

from .. import job_states
from ..categorized_errors import InvalidValue, MissingRecord
from ..logging import redact_text
from ..models import Job
from ..recipe_lifecycle_contract import RecipeOperationCancellationResult
from ..recipe_update_contract import (
    UPDATE_KIND,
    RecipeUpdateFailure,
    RecipeUpdateResponse,
    UpdateState,
    read_update_document,
)
from ..strict_json import serialize_json_value

if TYPE_CHECKING:
    from .service import RecipeUpdateBatches

from .helpers import _OBSERVATION_INTERVAL, _SETTLED, _now


class CancellationMixin:
    def cancel(
        self, operation_id: str, *, actor: str, request_id: str, reason: str
    ) -> RecipeUpdateResponse:
        """Stop future batch admissions and durably detach its exact children."""
        service = cast("RecipeUpdateBatches", self)

        with service.sessions.begin() as session:
            service._authorize(session, actor)
            job = session.scalar(
                select(Job)
                .where(Job.id == operation_id, Job.kind == UPDATE_KIND)
                .with_for_update(nowait=True)
            )
            if job is None:
                raise MissingRecord(operation_id)
            service.owner._request_cancellation(
                session,
                job,
                actor=actor,
                request_id=request_id,
                reason=reason,
                authorize=False,
            )
        return service.get(operation_id)

    def reconcile_cancellations(self, *, limit: int = 8) -> int:
        """Reconcile accepted child operations without claiming the batch slot."""
        service = cast("RecipeUpdateBatches", self)

        from ..recipe_image_availability import (
            RecipeImageAvailabilityError,
            RecipeImageAvailabilityView,
        )

        if not 1 <= limit <= 100:
            raise InvalidValue(
                "update cancellation limit is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        with service.sessions() as session:
            operation_ids = tuple(
                session.scalars(
                    select(Job.id)
                    .where(
                        Job.kind == UPDATE_KIND,
                        Job.state.in_(
                            job_states.words(LifecycleState.OBSERVING, kind=UPDATE_KIND)
                        ),
                    )
                    .order_by(Job.updated_at, Job.id)
                    .limit(limit)
                )
            )
        progressed = 0
        for operation_id in operation_ids:
            with service.sessions() as session:
                parent = session.get(Job, operation_id)
                if parent is None or parent.state not in job_states.words(
                    LifecycleState.OBSERVING, kind=UPDATE_KIND
                ):
                    continue
                try:
                    document = service._document(parent)
                except RecipeImageAvailabilityError:
                    document = None
                cancellation = None if document is None else document.cancellation
                actor = parent.actor
            if document is None or cancellation is None:
                # Unreadable cancel evidence ends the cancel (rule 4): the
                # document is retained untouched as the residue.
                if service._cancel_unreadable(operation_id):
                    progressed += 1
                continue
            observations: dict[str, RecipeImageAvailabilityView | None] = {}
            for child in document.children:
                if child.state in _SETTLED:
                    continue
                observed: RecipeImageAvailabilityView | None
                try:
                    value = service.owner.get_operator_request(
                        child.request_key, actor=actor
                    )
                except KeyError:
                    # Child admission and the parent's cancellation serialize on
                    # the same parent row. With cancellation holding that row,
                    # absence is durable evidence that this child never issued.
                    observed = None
                except RecipeImageAvailabilityError:
                    continue
                else:
                    if not isinstance(value, RecipeImageAvailabilityView):
                        continue
                    if (
                        value.request != service._intent(child)
                        or value.recipe_content_sha256 != child.recipe_content_sha256
                        or (
                            child.operation_id is not None
                            and child.operation_id != value.id
                        )
                    ):
                        continue
                    child_cancel_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"vonk:recipe-update-cancel:{operation_id}:{cancellation.cancel_request_id}:{child.request_key}",
                        )
                    )
                    child_cancellation = RecipeOperationCancellationResult(
                        cancel_requested=True,
                        cancel_requested_at=cancellation.cancel_requested_at,
                        cancel_request_id=child_cancel_id,
                        cancel_actor=cancellation.cancel_actor,
                        reason=cancellation.reason,
                    )
                    try:
                        service.owner._cancel_update_child(value.id, child_cancellation)
                        value = service.owner.get_operator_operation(value.id)
                    except RecipeImageAvailabilityError:
                        continue
                    except DBAPIError as error:
                        if getattr(error.orig, "sqlstate", None) == "55P03":
                            continue
                        raise
                    if not isinstance(value, RecipeImageAvailabilityView):
                        continue
                    observed = value
                observations[child.request_key] = observed
            # A child that could not be observed this pass stays as recorded; the
            # core's stop budget (not an endless retry) bounds the cancel.
            now = _now(service.owner._clock())
            try:
                with service.sessions.begin() as session:
                    parent = session.scalar(
                        select(Job)
                        .where(Job.id == operation_id, Job.kind == UPDATE_KIND)
                        .with_for_update(nowait=True)
                    )
                    if parent is None or parent.state not in job_states.words(
                        LifecycleState.OBSERVING, kind=UPDATE_KIND
                    ):
                        continue
                    current = service._document(parent)
                    if (
                        current.cancellation is None
                        or current.cancellation.cancel_request_id
                        != cancellation.cancel_request_id
                    ):
                        continue
                    children = []
                    for child in current.children:
                        observed = observations.get(child.request_key)
                        if (
                            child.state in _SETTLED
                            or child.request_key not in observations
                        ):
                            children.append(child)
                        elif observed is None:
                            children.append(
                                child.model_copy(
                                    update={
                                        "state": "cancelled",
                                        "failure": None,
                                        "retry_at": None,
                                        "observed_at": now,
                                    }
                                )
                            )
                        else:
                            state = cast(UpdateState, observed.state)
                            children.append(
                                child.model_copy(
                                    update={
                                        "operation_id": observed.id,
                                        "state": state,
                                        "failure": (
                                            None
                                            if observed.failure is None
                                            else RecipeUpdateFailure(
                                                code=str(observed.failure["code"]),
                                                detail=str(
                                                    redact_text(
                                                        str(observed.failure["detail"])
                                                    )
                                                )[:512],
                                                retryable=(
                                                    observed.failure.get("retryable")
                                                    is True
                                                ),
                                            )
                                        ),
                                        "retry_at": None,
                                        "observed_at": now,
                                    }
                                )
                            )
                    current = current.model_copy(
                        update={
                            "children": children,
                            "claim_owner": None,
                            "claim_until": None,
                            "next_attempt_at": (
                                None
                                if all(child.state in _SETTLED for child in children)
                                else now + _OBSERVATION_INTERVAL
                            ),
                        }
                    )
                    parent.payload = serialize_json_value(
                        read_update_document(serialize_json_value(current))
                    )
                    service._lifecycle.cancel_progress(
                        parent,
                        current,
                        now,
                        settled_children=all(
                            child.state in _SETTLED for child in children
                        ),
                        reason=cancellation.reason,
                    )
                    progressed += 1
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) == "55P03":
                    continue
                raise
        return progressed

    def _cancel_unreadable(self, operation_id: str) -> bool:
        """End a cancel whose stored document cannot be read (the effect is unknown)."""
        service = cast("RecipeUpdateBatches", self)

        try:
            with service.sessions.begin() as session:
                parent = session.scalar(
                    select(Job)
                    .where(Job.id == operation_id, Job.kind == UPDATE_KIND)
                    .with_for_update(nowait=True)
                )
                if parent is None or parent.state not in job_states.words(
                    LifecycleState.OBSERVING, kind=UPDATE_KIND
                ):
                    return False
                service._lifecycle.cancel_unreadable(
                    parent, _now(service.owner._clock())
                )
                return True
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "55P03":
                return False
            raise
