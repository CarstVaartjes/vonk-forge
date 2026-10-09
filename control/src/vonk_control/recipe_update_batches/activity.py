"""Recipe update batches: activity."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import func, select
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleSubject,
    OperationProgress,
)

from ..categorized_errors import InvalidValue, MissingRecord
from ..models import Job
from ..operation_api import (
    OperationListPage,
    OperationProvider,
    OperationQuery,
    _activity_keyset_filter,
)
from ..operation_contract import AvailabilityOperationFailure
from ..operation_item_contract import OperationItem, OperationOwnerReference
from ..recipe_update_contract import (
    UPDATE_KIND,
)
from ..state_filters import state_filter
from ..strict_json import serialize_json_value

if TYPE_CHECKING:
    from .service import RecipeUpdateBatches

from .helpers import _now


class ActivityMixin:
    def activity_provider(self) -> OperationProvider:
        service = cast("RecipeUpdateBatches", self)
        return OperationProvider(
            "recipe-update",
            service._activity_list,
            service._activity_get,
            represented_job_kinds=frozenset({UPDATE_KIND}),
        )

    def _activity_item(self, job: Job) -> OperationItem:
        service = cast("RecipeUpdateBatches", self)
        from ..recipe_image_availability import RecipeImageAvailabilityError

        owner = OperationOwnerReference(
            kind="job", id=job.id, request_id=job.request_id
        )
        try:
            view = service._view(job, service._document(job))
        except RecipeImageAvailabilityError as error:
            return OperationItem(
                id=job.id,
                owner=owner,
                kind=UPDATE_KIND,
                state=job.state,
                attempt=job.current_attempt,
                created_at=_now(job.created_at).isoformat(),
                updated_at=_now(job.updated_at).isoformat(),
                supported_actions=[],
                status_reason=error.detail,
                failure=AvailabilityOperationFailure(
                    code=error.code, detail=error.detail, retryable=False
                ),
            )
        failures = sum(
            child.state in {"failed", "cancelled"} for child in view.children
        )
        return OperationItem(
            id=view.id,
            owner=owner,
            kind=UPDATE_KIND,
            state=view.state,
            attempt=view.attempt,
            progress=OperationProgress.model_validate(
                serialize_json_value(view.progress)
            ),
            created_at=view.created_at.isoformat(),
            updated_at=view.updated_at.isoformat(),
            supported_actions=[],
            status_reason=f"{failures} of {len(view.children)} recipes failed or were cancelled; inspect with vonkctl recipe progress {view.id}"
            if failures
            else view.waiting_on,
        )

    def _activity_get(self, operation_id: str) -> OperationItem:
        service = cast("RecipeUpdateBatches", self)
        with service.sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or job.kind != UPDATE_KIND:
                raise MissingRecord(operation_id)
            return service._activity_item(job)

    def _activity_list(self, query: OperationQuery) -> OperationListPage:
        service = cast("RecipeUpdateBatches", self)
        if not 1 <= query.limit <= 101:
            raise InvalidValue(
                "operation provider page limit is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        if query.node_id is not None:
            return OperationListPage((), None, 0)
        filters = [Job.kind == UPDATE_KIND]
        if query.state is not None:
            filters.append(state_filter(Job.state, LifecycleSubject.JOB, query.state))
        if query.request_id is not None:
            filters.append(Job.request_id == query.request_id)
        with service.sessions() as session:
            total = (
                session.scalar(select(func.count()).select_from(Job).where(*filters))
                or 0
            )
            boundary = _activity_keyset_filter(Job.created_at, Job.id, "", query.after)
            if boundary is not None:
                filters.append(boundary)
            rows = session.scalars(
                select(Job)
                .where(*filters)
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(query.limit)
            )
            return OperationListPage(
                tuple(service._activity_item(job) for job in rows), None, total
            )
