"""Recipe update batches: projection."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from vonk_agent_protocol import (
    LifecycleState,
    OperationProgress,
    ProgressPhase,
)

from .. import job_states
from ..categorized_errors import MissingRecord
from ..models import Job
from ..recipe_update_contract import (
    UPDATE_KIND,
    RecipeUpdateDocument,
    RecipeUpdateResponse,
    UpdateState,
)

if TYPE_CHECKING:
    from .service import RecipeUpdateBatches

from .helpers import _SETTLED, _now


class ProjectionMixin:
    def _view(self, job: Job, document: RecipeUpdateDocument) -> RecipeUpdateResponse:
        complete = sum(child.state in _SETTLED for child in document.children)
        waiting = job.state in job_states.words(
            LifecycleState.QUEUED,
            LifecycleState.RUNNING,
            LifecycleState.OBSERVING,
            kind=UPDATE_KIND,
        ) and bool(document.children)
        partial = job_states.means(
            job.state, LifecycleState.FAILED, kind=UPDATE_KIND
        ) and any(
            child.state == LifecycleState.SUCCEEDED for child in document.children
        )
        return RecipeUpdateResponse(
            id=job.id,
            request_id=job.request_id,
            request=document.request,
            state=cast(UpdateState, job.state),
            partial=partial,
            attempt=job.current_attempt,
            children=document.children,
            cancellation=document.cancellation,
            progress=OperationProgress(
                phase=ProgressPhase.UPDATING if waiting else ProgressPhase.COMPLETED,
                completed_bytes=0,
                total_bytes_known=False,
                completed_items=complete,
                total_items=len(document.children),
            ),
            waiting_on="recipe operations" if waiting else None,
            wait_owner="recipe-image-availability" if waiting else None,
            next_attempt_at=document.claim_until or document.next_attempt_at,
            resume_condition="a child changes state or its next observation is due"
            if waiting
            else None,
            created_at=_now(job.created_at),
            updated_at=_now(job.updated_at),
        )

    def get(self, operation_id: str) -> RecipeUpdateResponse:
        service = cast("RecipeUpdateBatches", self)
        with service.sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or job.kind != UPDATE_KIND:
                raise MissingRecord(operation_id)
            return service._view(job, service._document(job))
