"""Removal completion for exact recipe image availability."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    LifecycleState,
    WaitReason,
)

from .. import job_states
from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    clear_removal,
    lock_removal_fences,
    retryable_artifact_database_error,
)
from ..artifact_reference_scan import (
    runtime_image_reference_reasons,
)
from ..models import (
    Job,
)
from ..recipe_image_removal_contract import (
    RecipeCacheRemovalOwner,
    RecipeCacheRemovalResult,
)
from ..strict_json import serialize_json_value
from .contracts import (
    REMOVE_OPERATION_KIND,
    SCHEMA_VERSION,
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityUnknown,
    _removal_retry_is_due,
    _retryable,
)

if TYPE_CHECKING:
    from .service import RecipeImageAvailabilityService


def _finish_recipe_removal(
    self: RecipeImageAvailabilityService,
    operation_id: str,
    observed_owner: RecipeCacheRemovalOwner,
    *,
    now: datetime,
) -> bool:
    intent = observed_owner.plan.intent
    identities = tuple(
        ArtifactIdentity("runtime-image", digest)
        for digest in observed_owner.plan.image_archives
    )
    try:
        with self._sessions.begin() as session:
            if identities and not lock_removal_fences(
                session,
                identities,
                owner_kind="recipe-image-job",
                owner_id=operation_id,
                fence=intent.removal_fence,
                now=now,
            ):
                return False
            operation = session.scalar(
                select(Job)
                .where(Job.id == operation_id, Job.kind == REMOVE_OPERATION_KIND)
                .execution_options(populate_existing=True)
                .with_for_update(nowait=True)
            )
            if operation is None or operation.state not in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.BACKOFF,
            ):
                return False
            owner = self._read_removal_owner(operation)
            checkpoint = owner.checkpoint
            if (
                owner.plan.intent != intent
                or checkpoint.image_index != len(owner.plan.image_archives)
                or checkpoint.image_pending_bytes is not None
                or checkpoint.model_index != len(owner.plan.model_children)
                or not _removal_retry_is_due(checkpoint.failure, now)
            ):
                return False
            intent_archives = tuple(owner.plan.image_archives)
            references = runtime_image_reference_reasons(session, intent_archives)
            if any(references[digest] for digest in intent_archives):
                raise RecipeImageAvailabilityUnknown(
                    ArtifactLifecycleCode.REFERENCE_CHANGED,
                    "a runtime image reference remains after the removal effects",
                    retryable=True,
                    recovery_actions=("retry",),
                    reason=WaitReason.SCOPE_CHANGED,
                )
            if identities:
                clear_removal(
                    session,
                    identities,
                    owner_kind="recipe-image-job",
                    owner_id=operation_id,
                    fence=intent.removal_fence,
                    now=now,
                )
            result = RecipeCacheRemovalResult(
                schema_version=SCHEMA_VERSION,
                action="remove",
                selector=intent.selector,
                request_key=intent.request_key,
                review_digest=intent.review_digest,
                operation_id=operation.id,
                recipe_revision_id=intent.recipe_revision_id,
                with_model=intent.with_model,
                state="succeeded",
                reclaimed_bytes=(
                    checkpoint.image_reclaimed_bytes + checkpoint.model_reclaimed_bytes
                ),
                model_removals=[
                    child.operation_id for child in owner.plan.model_children
                ],
                preserved=["profile-assignments", "spark-local-copies"]
                + ([] if intent.with_model else ["model-download"]),
                cancelled_operations=[],
                cancelled_builds=[],
                next_actions=[],
            )
            operation.result = serialize_json_value(result)
            self._lifecycle.succeed(operation, now)
            updated_owner = owner.model_copy(
                update={"checkpoint": checkpoint.model_copy(update={"failure": None})}
            )
            operation.payload = serialize_json_value(updated_owner)
            return True
    except ArtifactLifecycleError as error:
        return self._record_recipe_removal_failure(
            operation_id,
            code=error.code,
            detail=error.detail,
            retryable=error.retryable,
            retry_after_seconds=5,
        )
    except RecipeImageAvailabilityError as error:
        return self._record_recipe_removal_failure(
            operation_id,
            code=error.code,
            detail=error.detail,
            retryable=_retryable(error),
            retry_after_seconds=error.retry_after_seconds or 5,
        )
    except DBAPIError as error:
        translated = retryable_artifact_database_error(error)
        if translated is None:
            raise
        return self._record_recipe_removal_failure(
            operation_id,
            code=translated.code,
            detail=translated.detail,
            retryable=True,
            retry_after_seconds=5,
        )
