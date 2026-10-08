"""Bounded retry for digest-bound recipe operations."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    LifecycleState,
    ProgressPhase,
    ReservationState,
    UnknownOutcomeError,
)

from .. import job_states
from ..admission_locking import admission_attempts
from ..lifecycle.evidence import (
    Damaged,
    Residue,
    read_or_rebuild,
)
from ..models import (
    AgentOperation,
    InstallationNode,
    Job,
    RecipeBuild,
    RecipeInstallation,
    ResourceReservation,
)
from ..recipe_build_cancellation import (
    RecipeBuildIntent,
    build_cancellation,
)
from ..recipe_builds import (
    RecipeBuildPlan,
)
from ..recipe_execution_contract import (
    build_plan_document,
    parse_stored_build_plan,
)
from ..recipe_progress import (
    _parent_identity as _parent_identity,  # noqa: PLC0414 -- shared helper export
)
from ..stored_json import read_row_column
from .errors import RecipeRequestInvalid, RecipeRetryLater
from .intent import _bound_workload_intent
from .interfaces import RecipeOperationView
from .observation_helpers import _active_recipe_revision

if TYPE_CHECKING:
    from .service import RecipeOperationService


class RetryMixin:
    def retry(
        self, operation_id: str, *, actor: str, request_id: str
    ) -> RecipeOperationView:
        service = typing_cast("RecipeOperationService", self)
        last_error: UnknownOutcomeError | None = None
        for _attempt in admission_attempts():
            try:
                return service._retry_once(
                    operation_id, actor=actor, request_id=request_id
                )
            except UnknownOutcomeError as error:
                last_error = error
        assert last_error is not None
        raise last_error

    def _retry_once(
        self, operation_id: str, *, actor: str, request_id: str
    ) -> RecipeOperationView:
        service = typing_cast("RecipeOperationService", self)
        now = service._clock()
        with service._sessions.begin() as session:
            previous = session.get(Job, operation_id, with_for_update=True)
            previous_plan_digest = (
                _parent_identity(previous, "plan_digest")
                if previous is not None
                else None
            )
            if previous is None or previous_plan_digest is None:
                raise RecipeRequestInvalid("recipe operation is not retryable")
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is not None:
                if (
                    existing.kind != previous.kind
                    or _parent_identity(existing, "plan_digest") != previous_plan_digest
                ):
                    raise RecipeRequestInvalid("request key was already used")
                return service._view(existing)
            if previous.kind == WireAgentOperation.RECIPE_BUILD.value:
                job = service._retry_build_in_session(
                    session,
                    previous,
                    actor=actor,
                    request_id=request_id,
                    now=now,
                    intent=RecipeBuildIntent(kind="independent"),
                )
            elif (
                previous.kind == WireAgentOperation.RECIPE_INSTALL.value
                and previous.state == LifecycleState.FAILED.value
            ):
                job = service._retry_install_in_session(
                    session, previous, actor=actor, request_id=request_id, now=now
                )
            else:
                raise RecipeRequestInvalid("recipe operation is not retryable")
        service._agent_jobs.notify_available()
        return service.get(job.id)

    def _retry_install_in_session(
        self,
        session: Session,
        previous: Job,
        *,
        actor: str,
        request_id: str,
        now: datetime,
    ) -> Job:
        service = typing_cast("RecipeOperationService", self)
        previous_plan_digest = _parent_identity(previous, "plan_digest")
        owner_id = _parent_identity(previous, "owner_id")
        if previous_plan_digest is None or owner_id is None:
            raise RecipeRequestInvalid("recipe operation is not retryable")
        installation = session.get(RecipeInstallation, owner_id, with_for_update=True)
        if installation is None or installation.state not in {
            InstallationState.PARTIAL,
            InstallationState.FAILED,
        }:
            raise RecipeRequestInvalid("recipe installation is not retryable")
        nodes = tuple(
            session.scalars(
                select(InstallationNode)
                .where(InstallationNode.installation_id == installation.id)
                .order_by(InstallationNode.node_id)
            )
        )
        revision = _active_recipe_revision(session, installation.recipe_revision_id)
        assert revision is not None and revision.content_digest is not None
        recipe_digest = revision.content_digest
        compiled_plans = service._stored_compiled_plans(
            session, installation, [node.node_id for node in nodes], now=now
        )
        if isinstance(compiled_plans, Residue) or not nodes:
            raise RecipeRetryLater(
                "stored compiled execution plan is unreadable for install retry; it "
                "is recorded and the next preparation installs afresh"
            )
        installation.state = InstallationState.INSTALLING
        installation.updated_at = now
        for node in nodes:
            node.state = InstallationNodeState.PLANNED
        # The attempt that failed may have had its disk claim released as
        # abandoned (nothing was issued for it); the retry is that operation
        # again, so it takes the same exact claim back.
        for claim in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "installation",
                ResourceReservation.owner_id == owner_id,
                ResourceReservation.kind == "disk",
                ResourceReservation.state == ReservationState.RELEASED,
                ResourceReservation.plan_digest == previous_plan_digest,
            )
        ):
            claim.state = ReservationState.ACTIVE
            claim.released_at = None
        return service._queue_in_session(
            session,
            kind=WireAgentOperation.RECIPE_INSTALL.value,
            owner_kind="installation",
            owner_id=owner_id,
            plan_digest=previous_plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=tuple(
                (
                    node.node_id,
                    {
                        "installation_id": owner_id,
                        "plan_digest": previous_plan_digest,
                        "expected_bytes": node.required_bytes,
                        "compiled_execution_plan": compiled_plans[
                            node.node_id
                        ].model_dump(mode="json"),
                    },
                )
                for node in nodes
            ),
            authority_digest=recipe_digest,
            now=now,
            workload_intent_ordinal=_bound_workload_intent(previous),
        )

    def _retry_build_in_session(
        self,
        session: Session,
        previous: Job,
        *,
        actor: str,
        request_id: str,
        now: datetime,
        intent: RecipeBuildIntent,
    ) -> Job:
        service = typing_cast("RecipeOperationService", self)
        if previous.state not in job_states.words(
            LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR
        ):
            raise RecipeRequestInvalid("recipe build is not retryable")
        if build_cancellation(previous) is not None:
            raise RecipeRequestInvalid("cancelled recipe build intent is not retryable")
        if service._builds is None:
            raise RecipeRetryLater("recipe build service is unavailable")
        owner_id = _parent_identity(previous, "owner_id")
        build = (
            session.get(RecipeBuild, owner_id, with_for_update=True)
            if owner_id is not None
            else None
        )
        if (
            owner_id is None
            or build is None
            or build.state
            not in {ProgressPhase.BUILDING.value, LifecycleState.FAILED.value}
        ):
            raise RecipeRequestInvalid("recipe build is not retryable")
        active = next(
            (
                job
                for job in session.scalars(
                    select(Job)
                    .where(Job.kind == WireAgentOperation.RECIPE_BUILD.value)
                    .order_by(Job.id)
                )
                if job.id != previous.id
                and job.state
                in {LifecycleState.QUEUED.value, LifecycleState.RUNNING.value}
                and isinstance(read_row_column(job, "payload"), Mapping)
                and _parent_identity(job, "owner_id") == owner_id
            ),
            None,
        )
        if active is not None:
            # A retry is already in flight: this request adopts it.
            return active

        def plan_from(document: object) -> RecipeBuildPlan | Damaged:
            payload = build_plan_document(document)
            parsed_payload = parse_stored_build_plan(payload)
            if (
                payload.get("build_id") != owner_id
                or payload.get("build_input_sha256") != build.build_input_sha256
            ):
                return Damaged("stored build plan names another build")
            return RecipeBuildPlan(
                build_id=owner_id,
                recipe_revision_id=parsed_payload.recipe_revision_id,
                recipe_content_sha256=parsed_payload.recipe_content_sha256,
                builder_node_id=build.builder_node_id,
                source_bundle_sha256=parsed_payload.source_bundle_sha256,
                build_input_sha256=build.build_input_sha256,
                agent_payload=payload,
            )

        def rebuild() -> RecipeBuildPlan | None:
            # The build order the previous attempt carried is the plan it ran.
            child = session.scalar(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == previous.id)
                .order_by(AgentOperation.id)
                .limit(1)
            )
            if child is None:
                return None
            recovered = plan_from(read_row_column(child, "payload"))
            if isinstance(recovered, Damaged):
                return None
            build.plan = recovered.agent_payload
            return recovered

        loaded = read_or_rebuild(
            kind="recipe.build-plan",
            subject=owner_id,
            read=lambda: plan_from(read_row_column(build, "plan")),
            rebuild=rebuild,
        )
        if isinstance(loaded, Residue):
            raise RecipeRetryLater(
                "stored recipe build plan is unreadable; it is recorded and the next "
                "build request re-plans it"
            )
        plan = loaded
        service._release(session, "recipe-build", owner_id, now)
        return service._start_build_in_session(
            session,
            build,
            plan,
            actor=actor,
            request_id=request_id,
            now=now,
            intent=intent,
        )
