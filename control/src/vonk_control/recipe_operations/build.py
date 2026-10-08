"""Build for digest-bound recipe operations."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InstallationState,
    InvalidRequestReason,
    LifecycleState,
    ProgressPhase,
    UnknownOutcomeError,
    canonical_message,
)

from .. import job_states
from ..admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    acquire_admission_keys,
    admission_attempts,
    admission_wait_exhausted,
    job_request_key,
    lock_admission_rows,
    node_admission_key,
)
from ..agent_jobs import (
    _JsonFlagIsTrue,
)
from ..job_documents import (
    RecipeBuildParent,
)
from ..lifecycle.evidence import (
    BookkeepingReason,
    retire_as_unknown,
)
from ..models import (
    AgentNode,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
)
from ..prebuilt_images import policy_prebuilt_reference
from ..recipe_build_cancellation import (
    RecipeBuildIntent,
    build_cancellation,
    read_build_intent,
)
from ..recipe_builds import (
    RecipeBuildAdmissionBusy,
    RecipeBuildPlan,
)
from ..recipe_lifecycle_contract import (
    RecipeOperationCancellationResult,
)
from ..source_policy import SourcePolicyReport
from ..stored_json import read_row_column
from ..strict_json import serialize_json_value
from .errors import RecipeRequestInvalid, RecipeRetryLater
from .interfaces import RecipeOperationView, new_recipe_job
from .results import _recorded_result, _recorded_result_document

if TYPE_CHECKING:
    from .service import RecipeOperationService


class BuildMixin:
    def preview_build(
        self, recipe_revision_id: str, builder_node_id: str
    ) -> RecipeBuildPlan:
        service = typing_cast("RecipeOperationService", self)
        if service._builds is None:
            raise RecipeRetryLater("recipe build service is unavailable")
        return service._builds.plan(
            recipe_revision_id, builder_node_id, now=service._clock()
        )

    def reusable_build_id(self, recipe_revision_id: str) -> str | None:
        service = typing_cast("RecipeOperationService", self)
        if service._builds is None:
            return None
        return service._builds.reusable_build_id(recipe_revision_id)

    def check_build_source(self, recipe_revision_id: str) -> SourcePolicyReport:
        service = typing_cast("RecipeOperationService", self)
        last_error: UnknownOutcomeError | None = None
        for _attempt in admission_attempts():
            try:
                return service._check_build_source_once(recipe_revision_id)
            except UnknownOutcomeError as error:
                last_error = error
        assert last_error is not None
        raise last_error

    def _check_build_source_once(self, recipe_revision_id: str) -> SourcePolicyReport:
        service = typing_cast("RecipeOperationService", self)
        if service._builds is None:
            raise RecipeRetryLater("recipe build service is unavailable")
        return service._builds.check_source(recipe_revision_id)

    def _build_once(
        self,
        plan: RecipeBuildPlan,
        *,
        build_input_sha256: str,
        actor: str,
        request_id: str,
        force: bool = False,
        admission_guard: Callable[[Session], None] | None = None,
    ) -> RecipeOperationView:
        # A stale independent build request is re-planned with the current
        # builder inputs instead of being refused. A request that matches its
        # plan keeps that exact identity, and a parent Run/Switch request binds
        # the build identity in its accepted plan, so its admission guard keeps
        # it fixed. reserve_in_session rechecks current capacity either way.
        service = typing_cast("RecipeOperationService", self)
        if (
            service._builds is not None
            and admission_guard is None
            and build_input_sha256 != plan.build_input_sha256
        ):
            builder_node_id = plan.builder_node_id
            with service._sessions() as session:
                previous_build = session.get(RecipeBuild, plan.build_id)
                if (
                    previous_build is not None
                    and previous_build.recipe_revision_id == plan.recipe_revision_id
                ):
                    builder_node_id = previous_build.builder_node_id
            refreshed = service._builds.prepare_plan(
                plan.recipe_revision_id, builder_node_id, now=service._clock()
            )
            if (
                refreshed.builder_node_id != plan.builder_node_id
                or refreshed.build_input_sha256 != plan.build_input_sha256
            ):
                with service._sessions.begin() as session:
                    plan = service._builds.persist_plan_in_session(
                        session, refreshed, now=service._clock()
                    )
            build_input_sha256 = plan.build_input_sha256
        intent = RecipeBuildIntent(
            kind="dependency" if admission_guard is not None else "independent"
        )
        # Force bypasses cached images, not the identity of an accepted request.
        existing = service._idempotent(
            request_id, WireAgentOperation.RECIPE_BUILD.value, build_input_sha256
        )
        if existing is not None:
            if (
                existing.state
                not in {LifecycleState.SUCCEEDED.value, LifecycleState.CANCELLED.value}
                and not force
                and not isinstance(
                    existing.lifecycle_result, RecipeOperationCancellationResult
                )
            ):
                with service._sessions() as session:
                    succeeded = service._successful_build_job_in_session(
                        session, existing.owner_id, build_input_sha256
                    )
                    if succeeded is not None:
                        return service._view(succeeded)
            return existing
        now = service._clock()
        with service._sessions.begin() as session:
            try:
                acquire_admission_keys(
                    session,
                    (
                        job_request_key(request_id),
                        node_admission_key(plan.builder_node_id),
                    ),
                    holder="recipe-operation",
                )
                locked = lock_admission_rows(
                    session,
                    (
                        AdmissionRowLock(
                            "build-builder-node",
                            AgentNode,
                            select(AgentNode).where(
                                AgentNode.node_id == plan.builder_node_id
                            ),
                        ),
                        AdmissionRowLock(
                            "build-recipe-revision",
                            CatalogDocumentRevision,
                            select(CatalogDocumentRevision).where(
                                CatalogDocumentRevision.id == plan.recipe_revision_id
                            ),
                        ),
                        AdmissionRowLock(
                            "build-recipe-build",
                            RecipeBuild,
                            select(RecipeBuild).where(RecipeBuild.id == plan.build_id),
                        ),
                    ),
                )
            except AdmissionLockBusy as error:
                raise RecipeBuildAdmissionBusy() from error
            build = next(iter(locked["build-recipe-build"]), None)

            # A parent-owned build must revalidate its exact execution claim
            # in the same transaction that accepts the child. The guard does
            # SQL work only and retains its parent fence through this commit.
            if admission_guard is not None:
                admission_guard(session)
            if (
                build is not None
                and build.state == LifecycleState.SUCCEEDED.value
                and not force
                and build.build_input_sha256 == plan.build_input_sha256
                and build.builder_node_id == plan.builder_node_id
            ):
                succeeded = service._successful_build_job_in_session(
                    session, build.id, build.build_input_sha256
                )
                receipt = (
                    _recorded_result_document(
                        WireAgentOperation.RECIPE_BUILD.value,
                        read_row_column(succeeded, "result"),
                        subject=succeeded.id,
                    ).model_dump(mode="json")
                    if succeeded is not None
                    else None
                )
                if succeeded is None or receipt is None:
                    # The build row says succeeded but its receipt is gone: the
                    # evidence is rebuilt by building again (the same replacement
                    # a forced build performs), never by refusing the request.
                    retire_as_unknown(
                        "recipe.build-receipt",
                        build.id,
                        BookkeepingReason.ROW_INCOMPLETE,
                        "succeeded build has no successful receipt",
                    )
                    force = True
                else:
                    replay = new_recipe_job(
                        id=str(uuid.uuid4()),
                        request_id=request_id,
                        kind=succeeded.kind,
                        state=LifecycleState.SUCCEEDED.value,
                        actor=actor,
                        authority_revision=succeeded.authority_revision,
                        targets=list(succeeded.targets),
                        payload_digest=succeeded.payload_digest,
                        payload=RecipeBuildParent.model_validate_json(
                            canonical_message(read_row_column(succeeded, "payload"))
                        ).model_dump(mode="json")
                        | {"build_intent": intent.model_dump(mode="json")},
                        result=receipt,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(replay)
                    session.flush()
                    return service._view(replay)
            # Reusing a verified receipt above needs no new execution capacity.
            # A new attempt must wait for the cancelled executor's cleanup.
            cancelling = session.scalar(
                select(Job).where(
                    Job.kind == WireAgentOperation.RECIPE_BUILD.value,
                    Job.payload["owner_id"].as_string() == plan.build_id,
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    ),
                    _JsonFlagIsTrue(Job.result, "cancel_requested").is_(True),
                )
            )
            if cancelling is not None:
                build_cancellation(cancelling)
                raise RecipeRequestInvalid(
                    "recipe build cancellation is awaiting cleanup",
                    reason=InvalidRequestReason.NOT_READY,
                )
            if (
                build is None
                or build.build_input_sha256 != plan.build_input_sha256
                or build.builder_node_id != plan.builder_node_id
            ):
                raise RecipeRequestInvalid(
                    "recipe build preview is stale",
                    reason=InvalidRequestReason.SUPERSEDED,
                )
            if force and build.state == LifecycleState.SUCCEEDED.value:
                active = session.scalar(
                    select(Job)
                    .where(
                        Job.kind == WireAgentOperation.RECIPE_BUILD.value,
                        Job.state.in_(
                            (LifecycleState.QUEUED.value, LifecycleState.RUNNING.value)
                        ),
                        Job.payload["owner_id"].as_string() == build.id,
                        Job.payload["plan_digest"].as_string()
                        == build.build_input_sha256,
                    )
                    .order_by(Job.updated_at.desc())
                    .limit(1)
                )
                if active is not None:
                    if (
                        intent.kind == "independent"
                        and read_build_intent(active).kind == "dependency"
                    ):
                        # A distinct independent request cannot acquire intent
                        # by silently borrowing a parent's pending execution.
                        raise RecipeBuildAdmissionBusy()
                    return service._view(active)
                service._release(session, "recipe-build", build.id, now)
                job = service._start_build_in_session(
                    session,
                    build,
                    plan,
                    actor=actor,
                    request_id=request_id,
                    now=now,
                    force=True,
                    intent=intent,
                )
            elif build.state == LifecycleState.FAILED.value:
                previous = session.scalar(
                    select(Job)
                    .where(
                        Job.kind == WireAgentOperation.RECIPE_BUILD.value,
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.FAILED,
                                LifecycleState.NEEDS_OPERATOR,
                                LifecycleState.CANCELLED,
                            )
                        ),
                        Job.payload["owner_id"].as_string() == build.id,
                        Job.payload["plan_digest"].as_string()
                        == build.build_input_sha256,
                    )
                    .order_by(Job.updated_at.desc())
                    .limit(1)
                )
                if previous is None:
                    # A failed build with no failed receipt: the evidence is
                    # rebuilt by starting the build again.
                    retire_as_unknown(
                        "recipe.build-receipt",
                        build.id,
                        BookkeepingReason.ROW_INCOMPLETE,
                        "failed build has no failed receipt",
                    )
                    job = service._start_build_in_session(
                        session,
                        build,
                        plan,
                        actor=actor,
                        request_id=request_id,
                        now=now,
                        intent=intent,
                    )
                elif previous.state == LifecycleState.CANCELLED.value:
                    cancellation = build_cancellation(previous)
                    if cancellation is None or cancellation.cancelled is not True:
                        raise RecipeRequestInvalid(
                            "recipe build cancellation is awaiting cleanup",
                            reason=InvalidRequestReason.NOT_READY,
                        )
                    job = service._start_build_in_session(
                        session,
                        build,
                        plan,
                        actor=actor,
                        request_id=request_id,
                        now=now,
                        intent=intent,
                    )
                else:
                    job = service._retry_build_in_session(
                        session,
                        previous,
                        actor=actor,
                        request_id=request_id,
                        now=now,
                        intent=intent,
                    )
            elif build.state == InstallationState.PLANNED.value:
                job = service._start_build_in_session(
                    session,
                    build,
                    plan,
                    actor=actor,
                    request_id=request_id,
                    now=now,
                    intent=intent,
                )
            else:
                raise RecipeRequestInvalid(
                    "recipe build preview is stale",
                    reason=InvalidRequestReason.SUPERSEDED,
                )
        service._agent_jobs.notify_available()
        return service.get(job.id)

    def build(
        self,
        plan: RecipeBuildPlan,
        *,
        build_input_sha256: str,
        actor: str,
        request_id: str,
        force: bool = False,
        admission_guard: Callable[[Session], None] | None = None,
    ) -> RecipeOperationView:
        service = typing_cast("RecipeOperationService", self)
        refused: RecipeBuildAdmissionBusy | None = None
        for _attempt in admission_attempts():
            try:
                return service._build_once(
                    plan,
                    build_input_sha256=build_input_sha256,
                    actor=actor,
                    request_id=request_id,
                    force=force,
                    admission_guard=admission_guard,
                )
            except RecipeBuildAdmissionBusy as error:
                refused = error
                if admission_wait_exhausted(error):
                    break
        assert refused is not None
        raise refused

    def _start_build_in_session(
        self,
        session: Session,
        build: RecipeBuild,
        plan: RecipeBuildPlan,
        *,
        actor: str,
        request_id: str,
        now: datetime,
        intent: RecipeBuildIntent,
        force: bool = False,
    ) -> Job:
        service = typing_cast("RecipeOperationService", self)
        if service._builds is None:
            raise RecipeRetryLater("recipe build service is unavailable")
        prebuilt = policy_prebuilt_reference(read_row_column(build, "policy_report"))
        if prebuilt is not None:
            return service._queue_prebuilt_build_in_session(
                session,
                build,
                plan,
                prebuilt,
                actor=actor,
                request_id=request_id,
                now=now,
                intent=intent,
                force=force,
            )
        service._builds.reserve_in_session(
            session, plan, now=now, request_id=request_id
        )
        build.state = ProgressPhase.BUILDING.value
        build.error = None
        build.updated_at = now
        return service._queue_in_session(
            session,
            kind=WireAgentOperation.RECIPE_BUILD.value,
            owner_kind="recipe-build",
            owner_id=build.id,
            plan_digest=plan.build_input_sha256,
            actor=actor,
            request_id=request_id,
            node_payloads=((plan.builder_node_id, plan.agent_payload),),
            authority_digest=plan.build_input_sha256,
            now=now,
            job_context={
                "build_intent": intent.model_dump(mode="json"),
                **({"force_rebuild": True} if force else {}),
            },
        )

    def _queue_prebuilt_build_in_session(
        self,
        session: Session,
        build: RecipeBuild,
        plan: RecipeBuildPlan,
        reference: str,
        *,
        actor: str,
        request_id: str,
        now: datetime,
        intent: RecipeBuildIntent,
        force: bool,
    ) -> Job:
        """Queue a build the Controller executes by pulling a prebuilt image.

        It is an ordinary ``recipe.build.v1`` job that no Spark claims: no
        Spark operation or reservation is created. The prebuilt
        importer pulls the pinned digest and records the same evidence a Spark
        upload records, under the nominal builder named in the plan.
        """
        service = typing_cast("RecipeOperationService", self)
        try:
            acquire_admission_keys(session, (job_request_key(request_id),))
        except AdmissionLockBusy as error:
            raise RecipeBuildAdmissionBusy() from error
        existing = service._idempotent_job_in_session(
            session,
            request_id,
            WireAgentOperation.RECIPE_BUILD.value,
            plan.build_input_sha256,
            owner_kind="recipe-build",
            owner_id=build.id,
        )
        if existing is not None:
            return existing
        build.state = ProgressPhase.BUILDING.value
        build.error = None
        build.updated_at = now
        payload = RecipeBuildParent(
            schema_version=1,
            owner_kind="recipe-build",
            owner_id=build.id,
            plan_digest=plan.build_input_sha256,
            build_intent=intent,
            prebuilt_image=reference,
            prebuilt_node_id=build.builder_node_id,
            force_rebuild=force,
        )
        job = new_recipe_job(
            id=str(uuid.uuid4()),
            request_id=request_id,
            kind=WireAgentOperation.RECIPE_BUILD.value,
            state=LifecycleState.RUNNING.value,
            actor=actor,
            authority_revision=plan.build_input_sha256,
            # The nominal builder, like any build child; no Spark operation
            # is created, so no agent ever claims this job.
            targets=[build.builder_node_id],
            payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
            payload=serialize_json_value(payload),
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        return job

    @staticmethod
    def _successful_build_job_in_session(
        session: Session, build_id: str, build_input_sha256: str
    ) -> Job | None:
        job = session.scalar(
            select(Job)
            .where(
                Job.kind == WireAgentOperation.RECIPE_BUILD.value,
                Job.state == LifecycleState.SUCCEEDED.value,
                Job.payload["owner_id"].as_string() == build_id,
                Job.payload["plan_digest"].as_string() == build_input_sha256,
            )
            .order_by(Job.updated_at.desc())
            .limit(1)
        )
        return (
            job
            if job is not None
            and _recorded_result(
                job.kind, read_row_column(job, "result"), subject=job.id
            )
            is not None
            else None
        )
