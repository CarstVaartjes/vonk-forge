"""The lifecycle job owns cancellation of an exact build attempt."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict
from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    RecipeBuildCode,
    UnknownOutcomeError,
    canonical_message,
)
from vonk_forge_contracts import read_recipe

from . import job_states
from .agent_jobs import _JsonFlagIsTrue
from .categorized_errors import BookkeepingUnknown, InvalidValue
from .categorized_faults import StoredStateTypeDamaged
from .fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileAssignment,
    FleetProfilePreview,
    profile_switch_child_request_key,
)
from .models import (
    AgentNode,
    FleetProfileApplication,
    Job,
    RecipeBuild,
)
from .recipe_availability_intent import read_availability_intent
from .recipe_lifecycle_contract import (
    RecipeOperationCancellationResult,
    parse_recipe_lifecycle_result,
)
from .run_switch_contract import RunSwitchOperationResult, RunSwitchPlan
from .runtime_image_preparation import RuntimeImageReceipt
from .strict_json import StrictJSONModel, read_stored_model


class RecipeBuildIntent(StrictJSONModel):
    """The accepted producer's intent, independent of its current consumers."""

    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["independent", "dependency"]


def read_build_intent(job: Job) -> RecipeBuildIntent:
    try:
        if job.kind != "recipe.build.v1":
            raise InvalidValue(
                "build intent requires a build job",
                reason=InvalidRequestReason.INCOMPLETE,
            )
        return read_stored_model(
            RecipeBuildIntent,
            canonical_message(job.payload["build_intent"]),
            from_json=True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BuildConsumerError(
            RecipeBuildCode.PRODUCER_INVALID,
            "accepted build producer intent is invalid",
        ) from error


class BuildConsumerError(UnknownOutcomeError, RuntimeError):
    def __init__(self, code: str, detail: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(detail)


def needs_container_build(plan: RunSwitchPlan, phase_index: int = 0) -> bool:
    return any(
        phase.kind == "prepare"
        and phase.subphase == "container-build"
        and phase.state == "planned"
        and phase.index >= phase_index
        for phase in plan.phases
    )


def lock_run_switch_build_dependency(
    session: Session,
    plan: RunSwitchPlan,
    *,
    phase_index: int = 0,
    allow_cancelling: bool = False,
) -> None:
    if needs_container_build(plan, phase_index):
        lock_build_dependency(
            session,
            recipe_revision_id=plan.recipe_revision_id,
            builder_node_id=plan.build.builder_node_id,
            build_input_sha256=plan.build.build_input_sha256,
            build_id=plan.build.build_id,
            allow_cancelling=allow_cancelling,
        )


def lock_availability_build_dependency(
    session: Session, payload: Mapping[str, object]
) -> None:
    runtime = payload.get("runtime")
    if not isinstance(runtime, Mapping):
        raise BuildConsumerError(
            RecipeBuildCode.CONSUMER_INVALID, "availability runtime identity is invalid"
        )
    builder = runtime.get("builder_node_id")
    digest = payload.get("build_input_sha256")
    if builder is None or digest is None:
        return
    revision = payload.get("recipe_revision_id")
    if (
        not isinstance(builder, str)
        or not isinstance(digest, str)
        or not isinstance(revision, str)
    ):
        raise BuildConsumerError(
            RecipeBuildCode.CONSUMER_INVALID, "availability build identity is invalid"
        )
    lock_build_dependency(
        session,
        recipe_revision_id=revision,
        builder_node_id=builder,
        build_input_sha256=digest,
    )


def _profile_build_dependencies(
    review: FleetProfilePreview,
) -> tuple[tuple[FleetProfileAssignment, str], ...]:
    changing = {
        item.assignment_id
        for item in review.assignments
        if item.current_state != item.desired_state
    }
    assignments = {item.id: item for item in review.resolved_assignments}
    return tuple(
        (assignments[item.assignment_id], item.runtime_image.build_id)
        for item in review.preparation_decisions
        if item.assignment_id in changing and item.runtime_image.build_id is not None
    )


def lock_profile_build_dependencies(
    session: Session, review: FleetProfilePreview
) -> None:
    """Protect accepted exact images even before a recovery child is materialized."""
    try:
        dependencies = _profile_build_dependencies(review)
        for assignment, build_id in sorted(dependencies, key=lambda item: item[1]):
            build = session.get(RecipeBuild, build_id)
            if build is None:
                raise InvalidValue(
                    "reviewed profile build is missing",
                    reason=InvalidRequestReason.NOT_FOUND,
                )
            locked = lock_build_dependency(
                session,
                recipe_revision_id=assignment.recipe_revision_id,
                builder_node_id=build.builder_node_id,
                build_input_sha256=build.build_input_sha256,
                build_id=build_id,
            )
            if locked is None:
                raise InvalidValue("reviewed profile build disappeared")
    except (KeyError, TypeError, ValueError) as error:
        raise BuildConsumerError(
            RecipeBuildCode.CONSUMER_INVALID,
            "reviewed profile build identity is invalid",
        ) from error


def lock_build_dependency(
    session: Session,
    *,
    recipe_revision_id: str | None,
    builder_node_id: str | None,
    build_input_sha256: str | None,
    build_id: str | None = None,
    allow_cancelling: bool = False,
) -> RecipeBuild | None:
    """Serialize accepted dependency publication with cancellation of its build.

    A not-yet-resolved builder has no existing execution to depend on. Its
    selection transaction calls this again before publishing the exact identity.
    The caller keeps the lock through the accepted parent's commit.
    """
    if builder_node_id is None or build_input_sha256 is None:
        return None
    statement = select(RecipeBuild)
    if build_id is not None:
        statement = statement.where(RecipeBuild.id == build_id)
    else:
        statement = statement.where(
            RecipeBuild.recipe_revision_id == recipe_revision_id,
            RecipeBuild.builder_node_id == builder_node_id,
            RecipeBuild.build_input_sha256 == build_input_sha256,
        )
    try:
        build = session.scalar(
            statement.with_for_update(of=RecipeBuild, nowait=True).execution_options(
                populate_existing=True
            )
        )
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) != "55P03":
            raise
        raise BuildConsumerError(
            RecipeBuildCode.CONSUMER_BUSY,
            "build ownership is changing; retry the request",
            retryable=True,
        ) from error
    if build is None:
        return None
    if (
        build.builder_node_id != builder_node_id
        or build.build_input_sha256 != build_input_sha256
    ):
        raise BuildConsumerError(
            RecipeBuildCode.CONSUMER_INVALID, "accepted build consumer identity changed"
        )
    # Detachment removes demand; pending cleanup must not prevent it. New
    # consumers retain the default refusal and cannot join a cancelling child.
    if allow_cancelling:
        return build
    cancelling = session.scalar(
        select(Job).where(
            Job.kind == "recipe.build.v1",
            Job.payload["owner_id"].as_string() == build.id,
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
        try:
            build_cancellation(cancelling)
        except (TypeError, ValueError) as error:
            raise BuildConsumerError(
                RecipeBuildCode.CONSUMER_INVALID,
                "build cancellation evidence is invalid",
            ) from error
        raise BuildConsumerError(
            RecipeBuildCode.CANCELLATION_PENDING,
            "build cleanup must settle before joining this execution",
            retryable=True,
        )
    return build


def current_build_consumers(session: Session, build: RecipeBuild) -> tuple[str, ...]:
    """Derive dependencies from their accepted owners, under the build lock.

    Consumer publication uses lock_build_dependency. Removing demand is safe
    without that lock: at worst this snapshot conservatively refuses once.
    A malformed matching owner cannot authorize cancellation of its child.
    """
    candidates = session.scalars(
        select(Job)
        .where(
            or_(
                and_(
                    Job.kind == "recipe.run-switch.v2",
                    Job.state.in_(("queued", "running")),
                    or_(
                        Job.payload["plan"]["recipe_build_id"].as_string() == build.id,
                        Job.payload["plan"]["build"]["build_id"].as_string()
                        == build.id,
                    ),
                ),
                and_(
                    Job.kind == "recipe.image.availability.v2",
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.BACKOFF,
                        )
                    ),
                    Job.payload["runtime"]["builder_node_id"].as_string()
                    == build.builder_node_id,
                    Job.payload["build_input_sha256"].as_string()
                    == build.build_input_sha256,
                ),
            )
        )
        .order_by(Job.id)
    )
    consumers = []
    for parent in candidates:
        try:
            current = (
                _run_switch_consumer(session, parent, build)
                if parent.kind == "recipe.run-switch.v2"
                else _availability_consumer(parent, build)
            )
        except (KeyError, TypeError, ValueError) as error:
            raise BuildConsumerError(
                RecipeBuildCode.CONSUMER_INVALID,
                "accepted build consumer evidence is invalid",
            ) from error
        if current:
            consumers.append(parent.id)
    # Quoted identity containment is only a portable candidate filter. The
    # canonical document below decides ownership; unrelated malformed history
    # is not an admission barrier for this build.
    profiles = session.scalars(
        select(FleetProfileApplication)
        .where(
            FleetProfileApplication.state.in_(
                job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.NEEDS_OPERATOR,
                )
            ),
            cast(FleetProfileApplication.plan["preparation_decisions"], String).like(
                f'%"{build.id}"%'
            ),
        )
        .order_by(FleetProfileApplication.id)
    )
    for application in profiles:
        try:
            if _profile_consumer(session, application, build):
                consumers.append(application.id)
        except (KeyError, TypeError, ValueError) as error:
            raise BuildConsumerError(
                RecipeBuildCode.CONSUMER_INVALID,
                "accepted profile build evidence is invalid",
            ) from error
    return tuple(sorted(consumers))


def _profile_consumer(
    session: Session, application: FleetProfileApplication, build: RecipeBuild
) -> bool:
    review = read_stored_model(
        FleetProfilePreview, canonical_message(application.plan), from_json=True
    )
    progress = read_stored_model(
        FleetProfileApplicationProgress,
        canonical_message(application.progress),
        from_json=True,
    )
    if (
        review.profile_id != application.profile_id
        or review.profile_digest != application.profile_digest
        or review.plan_digest != application.plan_digest
        or progress.workload_intent_ordinal is None
    ):
        raise BookkeepingUnknown("profile build consumer identity is invalid")
    scope = {node_id for step in review.steps for node_id in step.node_ids}
    nodes = tuple(
        session.scalars(select(AgentNode).where(AgentNode.node_id.in_(scope)))
    )
    if len(nodes) != len(scope):
        raise BookkeepingUnknown("profile build consumer scope is missing")
    if any(
        node.workload_intent_ordinal != progress.workload_intent_ordinal
        for node in nodes
    ):
        return False
    for assignment, build_id in _profile_build_dependencies(review):
        if build_id != build.id:
            continue
        adapter = progress.switch_adapter
        if adapter is None:
            return True
        for position, item in enumerate(adapter.queue):
            if item.kind not in {"run", "install"} or item.id != assignment.id:
                continue
            if position < adapter.position:
                continue
            child = session.scalar(
                select(Job).where(
                    Job.request_id
                    == profile_switch_child_request_key(
                        application.id, position, item.kind, item.id
                    )
                )
            )
            if child is None:
                return True
            child_plan = read_stored_model(
                RunSwitchPlan, canonical_message(child.payload["plan"]), from_json=True
            )
            child_progress = read_stored_model(
                RunSwitchOperationResult,
                canonical_message(child.result),
                from_json=True,
            )
            if (
                child.kind != "recipe.run-switch.v2"
                or child_progress.profile_application_id != application.id
                or child_progress.workload_intent_ordinal
                != progress.workload_intent_ordinal
                or child_plan.recipe_revision_id != assignment.recipe_revision_id
                or {node.node_id for node in child_plan.spark_group.nodes}
                != {node.node_id for node in assignment.nodes}
                or child_plan.build.build_id != build.id
            ):
                raise BookkeepingUnknown(
                    "profile build consumer child identity changed"
                )
            # The exact committed child now owns this dependency. Its current
            # phase/cancellation is evaluated by _run_switch_consumer, including
            # a child committed before the profile checkpoint was written.
    return False


def _run_switch_consumer(session: Session, parent: Job, build: RecipeBuild) -> bool:
    plan = read_stored_model(
        RunSwitchPlan, canonical_message(parent.payload["plan"]), from_json=True
    )
    progress = read_stored_model(
        RunSwitchOperationResult, canonical_message(parent.result), from_json=True
    )
    if progress.cancellation is not None:
        return False
    ordinal = parent.payload["workload_intent_ordinal"]
    targets = tuple(node.node_id for node in plan.spark_group.nodes)
    if (
        type(ordinal) is not int
        or ordinal < 1
        or progress.workload_intent_ordinal != ordinal
        or set(targets) != set(parent.targets)
    ):
        raise BookkeepingUnknown("build consumer workload scope is invalid")
    nodes = tuple(
        session.scalars(select(AgentNode).where(AgentNode.node_id.in_(targets)))
    )
    if len(nodes) != len(targets):
        raise BookkeepingUnknown("build consumer workload scope is missing")
    if any(node.workload_intent_ordinal != ordinal for node in nodes):
        return False
    if (
        plan.build.build_id != build.id
        or plan.recipe_build_id not in (None, build.id)
        or plan.build.build_input_sha256 != build.build_input_sha256
        or plan.build.builder_node_id != build.builder_node_id
    ):
        raise BookkeepingUnknown("build consumer identity changed")
    return needs_container_build(plan, progress.phase_index)


def _availability_consumer(parent: Job, build: RecipeBuild) -> bool:
    payload = parent.payload
    if payload.get("removed") is True or payload.get("removal_fence") is not None:
        return False
    read_availability_intent(payload["request"])
    recipe = payload["recipe"]
    if not isinstance(recipe, Mapping):
        raise StoredStateTypeDamaged("availability recipe is invalid")
    read_recipe(recipe)
    runtime = payload["runtime"]
    if (
        not isinstance(runtime, Mapping)
        or runtime.get("build_input_sha256") != build.build_input_sha256
    ):
        raise BookkeepingUnknown("availability build consumer identity changed")
    image = payload.get("image_result")
    if image is not None:
        receipt = read_stored_model(
            RuntimeImageReceipt, canonical_message(image), from_json=True
        )
        if receipt.build_input_sha256 != build.build_input_sha256:
            raise BookkeepingUnknown("availability receipt identity changed")
        return False
    return True


def build_cancellation(job: Job) -> RecipeOperationCancellationResult | None:
    if job.kind != "recipe.build.v1":
        raise InvalidValue(
            "build cancellation requires a build job",
            reason=InvalidRequestReason.INCOMPLETE,
        )
    if job.result is None:
        return None
    result = parse_recipe_lifecycle_result(job.kind, job.result)
    return result if isinstance(result, RecipeOperationCancellationResult) else None


def request_build_cancellation(
    job: Job, *, actor: str, request_id: str, reason: str, now: datetime
) -> RecipeOperationCancellationResult:
    """Record intent with the caller's locked job, preserving the first request."""

    previous = build_cancellation(job)
    if previous is not None:
        return previous
    result = RecipeOperationCancellationResult(
        cancel_requested=True,
        cancel_requested_at=now,
        cancel_request_id=request_id,
        cancel_actor=actor,
        reason=reason,
    )
    job.result = result.model_dump(mode="json", exclude_none=True)
    job.status_reason = reason
    job.updated_at = now
    return result
