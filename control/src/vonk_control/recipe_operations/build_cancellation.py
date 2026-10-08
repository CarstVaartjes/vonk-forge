"""Build cancellation for digest-bound recipe operations."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    LifecycleState,
    ProgressPhase,
    RecipeBuildCleanupRequest,
    RecipeBuildCode,
    WaitReason,
)

from .. import job_states
from ..admission_locking import (
    admission_attempts,
    admission_wait_exhausted,
)
from ..job_documents import controller_recipe_document
from ..lifecycle import Outcome
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..lifecycle.evidence import (
    BookkeepingReason,
    retire_as_unknown,
)
from ..lifecycle.recipe_operation import RecipeOperationAdapter
from ..models import (
    AgentNode,
    AgentOperation,
    Job,
    RecipeBuild,
)
from ..prebuilt_images import prebuilt_reference
from ..recipe_build_cancellation import (
    BuildConsumerError,
    build_cancellation,
    current_build_consumers,
    read_build_intent,
    request_build_cancellation,
)
from ..recipe_progress import (
    _parent_force_rebuild as _parent_force_rebuild,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _parent_identity as _parent_identity,  # noqa: PLC0414 -- shared helper export
)
from ..strict_json import serialize_json_value
from .errors import RecipeBuildOwnershipBusy, RecipeRequestInvalid
from .observation_helpers import _aware

if TYPE_CHECKING:
    from .service import RecipeOperationService


class BuildCancellationMixin:
    def _cancel_build(
        self,
        job_id: str,
        *,
        actor: str,
        request_id: str,
        reason: str,
        only_if_unneeded: bool = False,
    ) -> bool:
        service = typing_cast("RecipeOperationService", self)
        refused: RecipeBuildOwnershipBusy | None = None
        for _attempt in admission_attempts():
            try:
                return service._cancel_current_build(
                    job_id,
                    actor=actor,
                    request_id=request_id,
                    reason=reason,
                    only_if_unneeded=only_if_unneeded,
                )
            except RecipeBuildOwnershipBusy as error:
                refused = error
                if admission_wait_exhausted(error):
                    break
        assert refused is not None
        raise refused

    def _cancel_current_build(
        self,
        job_id: str,
        *,
        actor: str,
        request_id: str,
        reason: str,
        only_if_unneeded: bool,
    ) -> bool:
        """One cancellation attempt; a lock held by another writer is retried."""
        service = typing_cast("RecipeOperationService", self)

        try:
            return service._cancel_current_build_locked(
                job_id,
                actor=actor,
                request_id=request_id,
                reason=reason,
                only_if_unneeded=only_if_unneeded,
            )
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) not in {
                "55P03",
                "40P01",
                "40001",
                "57014",
            }:
                raise
            raise RecipeBuildOwnershipBusy(
                f"{RecipeBuildCode.CONSUMER_BUSY}: build ownership is changing; retry cancellation",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error

    def _cancel_prebuilt_build(
        self,
        job_id: str,
        *,
        actor: str,
        request_id: str,
        reason: str,
        only_if_unneeded: bool,
    ) -> bool:
        """Cancel a Controller pull; nothing on a Spark needs cleaning up.

        An in-flight pull finds its job no longer running and discards its
        result; its partial file is removed by the importer.
        """
        service = typing_cast("RecipeOperationService", self)
        now = service._clock()
        with service._sessions.begin() as session:
            job = session.get(Job, job_id, with_for_update={"nowait": True})
            if job is None or job.state == LifecycleState.CANCELLED.value:
                return False
            if job.state not in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.NEEDS_OPERATOR,
            ):
                # A cancel always completes: a build that already ended has
                # nothing left to cancel.
                return False
            build_id = _parent_identity(job, "owner_id")
            build = (
                session.get(RecipeBuild, build_id, with_for_update={"nowait": True})
                if build_id is not None
                else None
            )
            if build is None:
                # The build row this pull belongs to is gone: the pull has
                # nothing to import into, so its cancellation is completed
                # on the job alone and the damage is retired as unknown.
                retire_as_unknown(
                    "recipe.build-cancel",
                    job.id,
                    BookkeepingReason.ROW_INCOMPLETE,
                    "the build row of a prebuilt pull is missing",
                )
                cancellation = request_build_cancellation(
                    job,
                    actor=actor,
                    request_id=request_id,
                    reason=reason,
                    now=_aware(now),
                )
                RecipeOperationAdapter().cancelled(job, now)
                job.result = serialize_json_value(
                    cancellation.model_copy(
                        update={LifecycleState.CANCELLED.value: True}
                    )
                )
                return True
            if build_cancellation(job) is None:
                if only_if_unneeded and read_build_intent(job).kind == "independent":
                    return False
                try:
                    consumers = current_build_consumers(session, build)
                except BuildConsumerError as error:
                    raise RecipeRequestInvalid(f"{error.code}: {error}") from error
                if consumers:
                    if only_if_unneeded:
                        return False
                    raise RecipeRequestInvalid(
                        f"{RecipeBuildCode.SHARED_CONSUMERS}: accepted preparation still needs this build; "
                        "cancel its parent intent first"
                    )
            cancellation = request_build_cancellation(
                job, actor=actor, request_id=request_id, reason=reason, now=_aware(now)
            )
            if build.state == ProgressPhase.BUILDING.value:
                build.state = (
                    LifecycleState.SUCCEEDED.value
                    if _parent_force_rebuild(job) is True
                    else LifecycleState.FAILED.value
                )
            build.error = cancellation.reason
            build.updated_at = now
            RecipeOperationAdapter().cancelled(job, now)
            job.result = serialize_json_value(
                cancellation.model_copy(update={LifecycleState.CANCELLED.value: True})
            )
            return True

    def _cancel_current_build_locked(
        self,
        job_id: str,
        *,
        actor: str,
        request_id: str,
        reason: str,
        only_if_unneeded: bool,
    ) -> bool:
        # Use the claim/result lock order: node, parent job, operation, build.
        service = typing_cast("RecipeOperationService", self)
        with service._sessions() as session:
            hinted = session.get(Job, job_id)
            if (
                hinted is not None
                and hinted.kind == WireAgentOperation.RECIPE_BUILD.value
                and prebuilt_reference(hinted) is not None
            ):
                return service._cancel_prebuilt_build(
                    job_id,
                    actor=actor,
                    request_id=request_id,
                    reason=reason,
                    only_if_unneeded=only_if_unneeded,
                )
            if hinted is None or hinted.kind != WireAgentOperation.RECIPE_BUILD.value:
                return False
            if len(hinted.targets) != 1:
                retire_as_unknown(
                    "recipe.build-cancel",
                    hinted.id,
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    "the build job does not name exactly one builder",
                )
                return False
            node_id = hinted.targets[0]
        now = service._clock()
        with service._sessions.begin() as session:
            node = session.get(AgentNode, node_id, with_for_update={"nowait": True})
            job = session.get(Job, job_id, with_for_update={"nowait": True})
            if node is None or job is None or job.targets != [node_id]:
                retire_as_unknown(
                    "recipe.build-cancel",
                    job_id,
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "the build's builder or job changed under the cancellation",
                )
                return False
            if job.state == LifecycleState.CANCELLED.value:
                return False
            if job.state not in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.NEEDS_OPERATOR,
            ):
                return False
            children = tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == job.id)
                    .order_by(AgentOperation.id)
                    .with_for_update(of=AgentOperation, nowait=True)
                )
            )
            if len(children) != 1 or children[0].node_id != node_id:
                retire_as_unknown(
                    "recipe.build-cancel",
                    job_id,
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "the build job's order does not match its builder",
                )
                return False
            child = children[0]
            build_owner = _parent_identity(job, "owner_id")
            # A lock held by another writer (SQLSTATE 55P03) reaches
            # ``_cancel_current_build``, which reports it for a bounded retry.
            build = (
                session.get(RecipeBuild, build_owner, with_for_update={"nowait": True})
                if build_owner is not None
                else None
            )
            if build is None or build.builder_node_id != node_id:
                retire_as_unknown(
                    "recipe.build-cancel",
                    job_id,
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "the build row is missing or names another builder",
                )
                return False
            if build_cancellation(job) is None:
                if only_if_unneeded and read_build_intent(job).kind == "independent":
                    return False
                try:
                    consumers = current_build_consumers(session, build)
                except BuildConsumerError as error:
                    raise RecipeRequestInvalid(f"{error.code}: {error}") from error
                if consumers:
                    if only_if_unneeded:
                        return False
                    raise RecipeRequestInvalid(
                        f"{RecipeBuildCode.SHARED_CONSUMERS}: accepted preparation still needs this build; "
                        "cancel its parent intent first"
                    )
            cancellation = request_build_cancellation(
                job, actor=actor, request_id=request_id, reason=reason, now=_aware(now)
            )
            # As with a failed replacement, cancellation keeps the last verified
            # image. Explicit cache removal independently invalidates its bytes.
            if build.state == ProgressPhase.BUILDING.value:
                build.state = (
                    LifecycleState.SUCCEEDED.value
                    if _parent_force_rebuild(job) is True
                    else LifecycleState.FAILED.value
                )
            build.error = cancellation.reason
            build.updated_at = now
            if child.current_attempt == 0:
                AgentOperationAdapter(session).record_outcome(
                    child, None, job, Outcome.CANCELLED, now
                )
                RecipeOperationAdapter().cancelled(job, now)
                job.result = serialize_json_value(
                    cancellation.model_copy(
                        update={LifecycleState.CANCELLED.value: True}
                    )
                )
                service._release_cancelled_build(session, build.id, now)
                return True
            cleanup_key = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"vonk:build-cleanup:{child.id}")
            )
            if session.scalar(select(Job.id).where(Job.request_id == cleanup_key)):
                return False
            payload = RecipeBuildCleanupRequest(
                build_id=build.id, operation_id=child.id
            )
            service._queue_in_session(
                session,
                kind=WireAgentOperation.RECIPE_BUILD_CLEANUP.value,
                owner_kind="recipe-build",
                owner_id=build.id,
                plan_digest=build.build_input_sha256,
                actor=actor,
                request_id=cleanup_key,
                node_payloads=((node_id, serialize_json_value(payload)),),
                authority_digest=build.build_input_sha256,
                now=now,
                job_context={
                    "build_cancellation": controller_recipe_document(cancellation)
                },
            )
        service._agent_jobs.notify_available()
        return True

    def _release_cancelled_build(
        self, session: Session, build_id: str, now: datetime
    ) -> None:
        service = typing_cast("RecipeOperationService", self)
        remaining = session.scalar(
            select(Job.id)
            .where(
                Job.kind == WireAgentOperation.RECIPE_BUILD.value,
                Job.payload["owner_id"].as_string() == build_id,
                Job.state.in_(
                    job_states.words(
                        LifecycleState.QUEUED,
                        LifecycleState.RUNNING,
                        LifecycleState.NEEDS_OPERATOR,
                    )
                ),
            )
            .limit(1)
        )
        if remaining is None:
            service._release(session, "recipe-build", build_id, now)
