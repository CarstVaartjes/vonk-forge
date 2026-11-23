from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    RecipeBuildCode,
    RecipeImageCode,
    SecurityRefusalError,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_forge_contracts import RecipeDefinition

from ..admission_locking import (
    AdmissionLockBusy,
    acquire_admission_keys,
    node_admission_key,
)
from ..bounded_json import require_mapping
from ..catalog.revision_contract import read_catalog_document
from ..categorized_errors import InvalidValue
from ..inventory.items import runtime_value
from ..jobs import states as job_states
from ..jobs.documents import AvailabilityJobPayload, AvailabilityRuntime
from ..models.cache import RecipeBuild
from ..models.catalog import CatalogDocumentRevision
from ..models.fleet import AgentNode
from ..models.jobs import Job
from ..models.operations import AgentOperation
from ..operations.blockers import make_blocker
from ..prebuilt_images import PrebuiltDecision
from ..recipe_builds.cancellation import BuildConsumerError, lock_build_dependency
from ..recipe_builds.common import (
    RecipeBuildAdmissionBusy,
    RecipeBuildResolution,
    RecipeSourcePolicyError,
)
from ..recipe_image_availability.build_observation import (
    accepted_build_request as _accepted_build_request,
)
from ..recipe_image_availability.build_observation import reconcile_abandoned_builds
from ..recipe_image_availability.contracts import (
    SOURCE_POLICY_REFUSED_CODE,
    BuildUnsettled,
    RecipeImageAvailabilityClaim,
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityRefused,
)
from ..recipe_image_availability.service import RecipeImageAvailabilityService
from ..recipe_operations.availability_intent import RecipeBuildDependency
from ..recipe_operations.errors import RecipeOperationConflict
from ..recipe_operations.runtime_specs import (
    resolve_recipe_entities,
)
from ..runtime_image_preparation.storage import FilesystemRuntimeImageStorage
from ..runtime_image_preparation.transport import OciLayoutImageTransport
from ..stored.states import CatalogRevisionState
from ..strict_json import read_stored_model
from .build_observation import (
    AvailabilityUnsettled,
    _cached_build_receipt,
    _capacity_wait,
    _compile_consistent_runtime,
    _observe_build,
    _prebuilt_blocker,
    _recorded_decision,
)
from .scheduler import RecipeImageAvailabilityScheduler

PREBUILT_PULL_KEY = "prebuilt_pull"


def _pulls_prebuilt_image(job: Job) -> bool:
    """Whether a build job is a Controller pull of a prebuilt image."""

    return isinstance(job.payload, Mapping) and isinstance(
        job.payload.get("prebuilt_image"), str
    )


_BUILDER_ADMISSION_CODES = frozenset(
    {
        RecipeBuildCode.NODE_UNKNOWN,
        RecipeBuildCode.NODE_INCOMPATIBLE,
        RecipeBuildCode.CAPACITY_BUSY,
        RecipeBuildCode.INVENTORY_MISSING,
        RecipeBuildCode.INVENTORY_STALE,
        RecipeBuildCode.CAPABILITY_MISSING,
        RecipeBuildCode.NETWORK_CAPABILITY_MISSING,
        RecipeBuildCode.INSUFFICIENT_DISK,
        RecipeBuildCode.INSUFFICIENT_MEMORY,
        RecipeBuildCode.RUNTIME_CHANGED,
    }
)


@dataclass(frozen=True, slots=True)
class RecipeImageAvailabilityProduction:
    """Concrete production services and their independently owned scheduler."""

    service: RecipeImageAvailabilityService
    scheduler: RecipeImageAvailabilityScheduler | None
    storage: FilesystemRuntimeImageStorage
    transport: OciLayoutImageTransport

    def close(self) -> None:
        if self.scheduler is not None:
            self.scheduler.close()


def build_recipe_image_availability(
    sessions: sessionmaker[Session],
    *,
    settings: Any | None = None,
    artifact_root: Any | None = None,
    recipe_builds: Any,
    recipe_operations: Any,
    model_cache: Any | None = None,
    clock: Callable[[], datetime],
    max_parallel: int = 4,
    with_scheduler: bool = False,
    storage: FilesystemRuntimeImageStorage,
) -> RecipeImageAvailabilityProduction:
    """Compose immutable catalog resolution, OCI storage and image execution."""

    image_root = artifact_root or getattr(settings, "agent_artifact_root", None)
    if image_root is None:
        raise InvalidValue(
            "recipe image artifact root is required",
            reason=InvalidRequestReason.INCOMPLETE,
        )
    transport = OciLayoutImageTransport()

    def authority(
        recipe_revision_id: str,
        *,
        force: bool = False,
    ) -> tuple[RecipeDefinition, AvailabilityRuntime]:
        with sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.id == recipe_revision_id,
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == CatalogRevisionState.ACTIVE,
                )
            )
            if revision is None:
                raise AvailabilityUnsettled(
                    RecipeImageCode.RECIPE_UNAVAILABLE,
                    "selected recipe revision is unavailable or inactive",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            try:
                recipe = read_catalog_document(revision)
                assert isinstance(recipe, RecipeDefinition)
                entities = resolve_recipe_entities(session, revision)
            except SecurityRefusalError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                raise AvailabilityUnsettled(
                    RecipeImageCode.RECIPE_UNAVAILABLE,
                    "exact recipe source or dependencies are unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            resolved_revision_id = revision.id
            session.close()
            package_handle: Mapping[str, object] | None = None
            resolution = None
            resolve = getattr(recipe_builds, "resolve", None)
            if isinstance(resolve, Callable):
                try:
                    resolution = resolve(recipe_revision_id)
                except SecurityRefusalError:
                    raise
                except RecipeSourcePolicyError as error:
                    raise RecipeImageAvailabilityRefused(
                        SOURCE_POLICY_REFUSED_CODE, str(error)[:512]
                    ) from error
                except Exception as error:
                    raise AvailabilityUnsettled(
                        str(getattr(error, "code", RecipeImageCode.BUILD_UNAVAILABLE)),
                        str(error)[:512],
                        retryable=True,
                        recovery_actions=("retry",),
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    ) from error
            if resolution is not None:
                cached = None if force else _cached_build_receipt(resolution)
                if cached is not None:
                    package_handle = {
                        "build_input_sha256": cached["build_input_sha256"],
                        "image_digest": str(cached["image_digest"]).removeprefix(
                            "sha256:"
                        ),
                        "image_reference": (
                            f"localhost/vonk/recipe-build@{cached['image_digest']}"
                        ),
                    }
                else:
                    package_handle = {
                        "input_intent_sha256": resolution.input_intent_sha256,
                        "image_digest": resolution.input_intent_sha256,
                        "image_reference": (
                            "localhost/vonk/recipe-build@sha256:"
                            f"{resolution.input_intent_sha256}"
                        ),
                    }
            try:
                runtime = _compile_consistent_runtime(
                    recipe,
                    resolved=entities,
                    package_handle=package_handle,
                )
                result = dict(runtime) | {"recipe_revision_id": resolved_revision_id}
                if package_handle is not None and isinstance(
                    package_handle.get("build_input_sha256"), str
                ):
                    result["build_input_sha256"] = package_handle["build_input_sha256"]
                if package_handle is not None and isinstance(
                    package_handle.get("input_intent_sha256"), str
                ):
                    result["input_intent_sha256"] = package_handle[
                        "input_intent_sha256"
                    ]
                return recipe, read_stored_model(AvailabilityRuntime, result)
            except SecurityRefusalError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                raise AvailabilityUnsettled(
                    RecipeImageCode.RUNTIME_INVALID,
                    "compiled runtime projection is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error

    def reobserve_authority(
        recipe_revision_id: str,
        *,
        force: bool = False,
    ) -> tuple[RecipeDefinition, AvailabilityRuntime]:
        # A recipe the catalog does not hold yet waits for the worker's next
        # sync; this only rides out a local read that failed once.
        unknown: AvailabilityUnsettled | None = None
        for _attempt in range(3):
            try:
                return authority(recipe_revision_id, force=force)
            except AvailabilityUnsettled as error:
                unknown = error
        assert unknown is not None
        raise unknown

    def recover_build(
        claim: RecipeImageAvailabilityClaim,
        *,
        join_active: bool = True,
        resolution: RecipeBuildResolution | None = None,
    ) -> tuple[str, str, str] | BuildUnsettled | None:
        """Recover committed work before source, inventory or capacity planning.

        ``None`` when nothing is committed to recover; a
        :class:`BuildUnsettled` when what is committed does not settle (the
        caller returns it, nothing was written)."""

        with sessions.begin() as session:
            parent = service._require_claim(session, claim)
            payload = parent.payload
            runtime = payload.get("runtime")
            if not isinstance(runtime, Mapping):
                return BuildUnsettled(
                    RecipeImageCode.OPERATION_INVALID,
                    "accepted build runtime is invalid",
                    reason=WaitReason.STALE_PLAN,
                )
            dependency = _build_dependency(payload)
            if isinstance(dependency, BuildUnsettled):
                return dependency
            child = None
            if dependency is not None:
                child = (
                    session.get(Job, str(dependency.operation_id))
                    if dependency.operation_id is not None
                    else session.scalar(
                        select(Job).where(Job.request_id == str(dependency.request_key))
                    )
                )
                if child is None:
                    # A lost child-id checkpoint is not lost intent. Reconcile
                    # the deterministic request before dispatching a replacement.
                    child = session.scalar(
                        select(Job).where(
                            Job.request_id == str(dependency.request_key),
                            Job.kind == WireAgentOperation.RECIPE_BUILD.value,
                        )
                    )
                    dependency = dependency.model_copy(update={"operation_id": None})
                    accepted = service._payload(parent)
                    if isinstance(accepted, AvailabilityJobPayload):
                        parent.payload = accepted.model_copy(
                            update={"build_dependency": dependency}
                        ).model_dump(mode="json", exclude_none=True)
                        payload = parent.payload
            builder_id = runtime.get("builder_node_id")
            digest = payload.get("build_input_sha256")
            revision_id = payload.get("recipe_revision_id")
            if (
                child is None
                and join_active
                and builder_id is None
                and resolution is not None
            ):
                if (
                    revision_id != resolution.recipe_revision_id
                    or runtime.get("input_intent_sha256")
                    != resolution.input_intent_sha256
                ):
                    return BuildUnsettled(
                        RecipeImageCode.IDENTITY_CONFLICT,
                        "accepted build intent no longer matches its resolution",
                        reason=WaitReason.SCOPE_CHANGED,
                    )
                # Selecting new capacity would count an accepted build's own
                # reservation against its consumers. Resolve an active exact
                # execution first, then join under the same cancellation fence
                # used by consumers that already know their builder.
                candidates = session.scalars(
                    select(RecipeBuild)
                    .join(Job, Job.owner_id == RecipeBuild.id)
                    .where(
                        RecipeBuild.recipe_revision_id == revision_id,
                        Job.kind == "recipe.build.v1",
                        Job.state.in_(job_states.IN_FLIGHT),
                    )
                    .order_by(RecipeBuild.created_at, RecipeBuild.id)
                ).unique()
                for candidate in candidates:
                    request = _accepted_build_request(session, candidate)
                    if request is None:
                        # A damaged projection cannot hide healthy candidates.
                        continue
                    if (
                        request.build_input_sha256 != candidate.build_input_sha256
                        or request.source_bundle_sha256
                        != resolution.source_bundle_sha256
                        or request.recipe_content_sha256
                        != resolution.recipe_content_sha256
                    ):
                        continue
                    builder_id = candidate.builder_node_id
                    digest = candidate.build_input_sha256
                    runtime = dict(runtime) | {
                        "builder_node_id": builder_id,
                        "build_input_sha256": digest,
                    }
                    payload = dict(payload) | {
                        "runtime": runtime,
                        "build_input_sha256": digest,
                        "identity_key": digest,
                    }
                    break
            if (
                child is None
                and join_active
                and isinstance(builder_id, str)
                and isinstance(digest, str)
            ):
                # A concurrent parent may have committed the same execution.
                # Join only under the consumer/cancellation ownership fence.
                try:
                    build = lock_build_dependency(
                        session,
                        recipe_revision_id=revision_id
                        if isinstance(revision_id, str)
                        else None,
                        builder_node_id=builder_id,
                        build_input_sha256=digest,
                    )
                except BuildConsumerError as error:
                    return _dependency_unsettled(error)
                if build is not None:
                    child = session.scalar(
                        select(Job)
                        .where(
                            Job.kind == "recipe.build.v1",
                            Job.owner_id == build.id,
                            Job.payload["plan_digest"].as_string() == digest,
                            Job.state.in_(job_states.IN_FLIGHT),
                        )
                        .order_by(Job.created_at, Job.id)
                        .limit(1)
                    )
            if child is None:
                return None
            owner = session.get(RecipeBuild, child.payload.get("owner_id"))
            if (
                child.kind != "recipe.build.v1"
                or owner is None
                or owner.builder_node_id != builder_id
                or owner.build_input_sha256 != digest
                or child.payload.get("plan_digest") != digest
                or child.targets != [builder_id]
                or not isinstance(builder_id, str)
                or not isinstance(digest, str)
            ):
                return BuildUnsettled(
                    RecipeImageCode.BUILD_INVALID,
                    "accepted build child identity changed",
                    retryable=True,
                    reason=WaitReason.SCOPE_CHANGED,
                )
            dependency = dependency or _new_build_dependency(parent)
            dependency = dependency.model_copy(
                update={"operation_id": uuid.UUID(child.id)}
            )
            parent.payload = dict(payload) | {
                "build_dependency": dependency.model_dump(
                    mode="json", exclude_none=True
                )
            }
            return child.id, builder_id, digest

    def builder(
        recipe: RecipeDefinition,
        runtime: Mapping[str, object],
        *,
        claim: RecipeImageAvailabilityClaim,
        build_input_sha256: str,
        force: bool,
        progress: Callable[[Mapping[str, object]], None],
    ) -> Mapping[str, object] | BuildUnsettled:
        del recipe
        reconcile_abandoned_builds(service, claim, recipe_operations)
        recovered = recover_build(claim, join_active=False)
        if isinstance(recovered, BuildUnsettled):
            return recovered
        if recovered is not None:
            child_id, child_builder, child_input = recovered
            return _observe_build(
                sessions,
                recipe_operations.get(child_id),
                builder_node_id=child_builder,
                build_input_sha256=child_input,
                progress=progress,
                storage=storage,
            )
        revision_id = runtime.get("recipe_revision_id")
        raw_builder_node_id = runtime.get("builder_node_id")
        builder_node_id: str | None = (
            raw_builder_node_id if isinstance(raw_builder_node_id, str) else None
        )
        if not isinstance(revision_id, str):
            return BuildUnsettled(
                RecipeImageCode.BUILD_INPUT_MISSING,
                "canonical build plan is unavailable after restart",
                reason=WaitReason.RECEIPT_MISSING,
            )
        resolution = recipe_builds.resolve(revision_id)
        cached = None if force else _cached_build_receipt(resolution)
        if cached is not None and build_input_sha256 in {
            "",
            cached["build_input_sha256"],
        }:
            return cached
        recovered = recover_build(claim, resolution=resolution)
        if isinstance(recovered, BuildUnsettled):
            return recovered
        if recovered is not None:
            child_id, child_builder, child_input = recovered
            return _observe_build(
                sessions,
                recipe_operations.get(child_id),
                builder_node_id=child_builder,
                build_input_sha256=child_input,
                progress=progress,
                storage=storage,
            )
        # SQL may still record a succeeded build whose archive is gone. The
        # resolution reports that as stale cache loss, and dispatch must build
        # again instead of replaying the vanished result.
        force = force or bool(getattr(resolution, "stale_receipt", False))
        selected_plan: Any | None = None
        selected_candidate: str | None = None
        candidate_ids: tuple[str, ...] = ()
        attempted_candidates: set[str] = set()
        # Why each Spark was passed over, so a wait names its causes.
        skipped: dict[str, tuple[str, str]] = {}
        prebuilt_unused: PrebuiltDecision | None = None
        if not isinstance(builder_node_id, str):
            # Read the parent and choose a candidate in a short transaction.
            # The candidate is locked again only for final persistence, after
            # all source/policy/inventory work has completed.
            with sessions.begin() as session:
                parent = service._require_claim(session, claim)
                parent_payload = parent.payload
                parent_runtime = require_mapping(parent_payload["runtime"], "runtime")
                if isinstance(parent_runtime, Mapping) and isinstance(
                    parent_runtime.get("builder_node_id"), str
                ):
                    builder_node_id = str(parent_runtime["builder_node_id"])
                else:
                    candidates = tuple(
                        candidate
                        for candidate in session.scalars(
                            select(AgentNode)
                            .where(AgentNode.state == NodeIdentityState.ACTIVE)
                            .order_by(AgentNode.node_id)
                        )
                        if runtime_value(
                            candidate, lambda identity: identity.architecture
                        )
                        == "linux-arm64"
                    )
                    candidate_ids = tuple(candidate.node_id for candidate in candidates)
                    occupied: list[str] = []

                    def _same_image(job: Job) -> bool:
                        # Exact same content is this request's own image, not other
                        # work: identical requests share it and never wait on each
                        # other's retry clock (content identity, request-led).
                        if job.id == parent.id:
                            return True
                        other = job.payload
                        if job.kind != "recipe.image.availability.v2" or not isinstance(
                            other, Mapping
                        ):
                            return False
                        mine = parent_payload
                        if other.get("recipe_content_sha256") != mine.get(
                            "recipe_content_sha256"
                        ):
                            return False
                        build = mine.get("build_input_sha256")
                        if (
                            build is not None
                            and other.get("build_input_sha256") == build
                        ):
                            return True
                        intent = parent_runtime.get("input_intent_sha256")
                        other_runtime = other.get("runtime")
                        return (
                            intent is not None
                            and isinstance(other_runtime, Mapping)
                            and other_runtime.get("input_intent_sha256") == intent
                        )

                    def work_for(node_id: str, jobs: tuple[Job, ...]) -> int:
                        return sum(
                            1
                            for job in jobs
                            if not _same_image(job)
                            if (
                                job.kind == "recipe.build.v1"
                                and node_id in (job.targets or ())
                                and not _pulls_prebuilt_image(job)
                                and session.scalar(
                                    select(AgentOperation.id)
                                    .where(
                                        AgentOperation.parent_job_id == job.id,
                                        AgentOperation.state.in_(
                                            job_states.words(
                                                LifecycleState.QUEUED,
                                                LifecycleState.RUNNING,
                                                LifecycleState.BACKOFF,
                                                LifecycleState.OBSERVING,
                                            )
                                        ),
                                    )
                                    .limit(1)
                                )
                                is not None
                            )
                            or (
                                job.kind == "recipe.image.availability.v2"
                                and isinstance(job.payload, Mapping)
                                and job.payload.get(PREBUILT_PULL_KEY) is not True
                                and not isinstance(
                                    job.payload.get("image_result"), Mapping
                                )
                                and isinstance(
                                    (job_runtime := job.payload.get("runtime")),
                                    Mapping,
                                )
                                and job_runtime.get("builder_node_id") == node_id
                            )
                        )

                    for candidate_id in candidate_ids:
                        try:
                            acquire_admission_keys(
                                session,
                                (node_admission_key(candidate_id),),
                                holder="image-availability",
                            )
                        except AdmissionLockBusy as error:
                            skipped[candidate_id] = (
                                RecipeImageCode.BUILDER_BUSY,
                                str(error),
                            )
                            continue
                        locked = session.scalar(
                            select(AgentNode)
                            .where(AgentNode.node_id == candidate_id)
                            .with_for_update(skip_locked=True)
                        )
                        if locked is None:
                            skipped[candidate_id] = (
                                RecipeImageCode.BUILDER_BUSY,
                                "another change to this Spark is in progress",
                            )
                            continue
                        current_jobs = tuple(
                            session.scalars(
                                select(Job).where(
                                    Job.state.in_(
                                        job_states.words(
                                            LifecycleState.QUEUED,
                                            LifecycleState.RUNNING,
                                            LifecycleState.BACKOFF,
                                        )
                                    ),
                                    Job.kind.in_(
                                        {
                                            "recipe.build.v1",
                                            "recipe.image.availability.v2",
                                        }
                                    ),
                                )
                            )
                        )
                        if work_for(candidate_id, current_jobs) > 0:
                            skipped[candidate_id] = (
                                RecipeImageCode.BUILDER_OCCUPIED,
                                "already building or preparing another image",
                            )
                            occupied.append(candidate_id)
                            continue
                        selected_candidate = candidate_id
                        break
                    if selected_candidate is None and occupied:
                        # The catalog's prebuilt image is pulled by the
                        # Controller and needs no free Spark, so an occupied
                        # Spark is still a candidate: the plan decides below,
                        # and a Spark build still waits for a free builder.
                        selected_candidate = occupied[0]
                    if selected_candidate is None:
                        return _capacity_wait(skipped, candidate_ids)
        while selected_plan is None and selected_candidate is not None:
            candidate_id = selected_candidate
            try:
                prepared = recipe_builds.prepare_plan(
                    revision_id,
                    candidate_id,
                    now=clock(),
                    resolution=resolution,
                )
            except (UnknownOutcomeError, RecipeImageAvailabilityError) as error:
                code = str(getattr(error, "code", ""))
                if code in _BUILDER_ADMISSION_CODES:
                    skipped[candidate_id] = (code, str(error)[:200])
                    prebuilt_unused = (
                        getattr(error, "prebuilt_unused", None) or prebuilt_unused
                    )
                    attempted_candidates.add(candidate_id)
                    selected_candidate = next(
                        (
                            other_id
                            for other_id in candidate_ids
                            if other_id != candidate_id
                            and other_id not in attempted_candidates
                        ),
                        None,
                    )
                    continue
                return _planning_failure(error)
            # Admission can still refuse the plan when it is persisted; the
            # wait then names why this was a Spark build at all.
            planned_decision = _recorded_decision(prepared.policy_report)
            if planned_decision is not None and not planned_decision.used:
                prebuilt_unused = planned_decision
            pulls_prebuilt = planned_decision is not None and planned_decision.used
            with sessions.begin() as session:
                try:
                    acquire_admission_keys(
                        session,
                        (node_admission_key(candidate_id),),
                        holder="image-availability",
                    )
                except AdmissionLockBusy as error:
                    skipped[candidate_id] = (
                        RecipeImageCode.BUILDER_BUSY,
                        str(error),
                    )
                    attempted_candidates.add(candidate_id)
                    selected_candidate = next(
                        (
                            other_id
                            for other_id in candidate_ids
                            if other_id != candidate_id
                            and other_id not in attempted_candidates
                        ),
                        None,
                    )
                    continue
                parent = service._require_claim(session, claim)
                parent_payload = parent.payload
                parent_runtime = require_mapping(parent_payload["runtime"], "runtime")
                assigned = (
                    parent_runtime.get("builder_node_id")
                    if isinstance(parent_runtime, Mapping)
                    else None
                )
                if isinstance(assigned, str):
                    builder_node_id = assigned
                    selected_candidate = None
                    continue
                locked = session.scalar(
                    select(AgentNode)
                    .where(AgentNode.node_id == candidate_id)
                    .with_for_update(skip_locked=True)
                )
                if locked is None:
                    skipped[candidate_id] = (
                        RecipeImageCode.BUILDER_BUSY,
                        "another change to this Spark is in progress",
                    )
                    attempted_candidates.add(candidate_id)
                    selected_candidate = next(
                        (
                            other_id
                            for other_id in candidate_ids
                            if other_id != candidate_id
                            and other_id not in attempted_candidates
                        ),
                        None,
                    )
                    continue
                current_jobs = tuple(
                    session.scalars(
                        select(Job).where(
                            Job.state.in_(
                                job_states.words(
                                    LifecycleState.QUEUED,
                                    LifecycleState.RUNNING,
                                    LifecycleState.BACKOFF,
                                )
                            ),
                            Job.kind.in_(
                                {"recipe.build.v1", "recipe.image.availability.v2"}
                            ),
                        )
                    )
                )
                if work_for(candidate_id, current_jobs) > 0 and not pulls_prebuilt:
                    skipped[candidate_id] = (
                        RecipeImageCode.BUILDER_OCCUPIED,
                        "already building or preparing another image",
                    )
                    attempted_candidates.add(candidate_id)
                    selected_candidate = next(
                        (
                            other_id
                            for other_id in candidate_ids
                            if other_id != candidate_id
                            and other_id not in attempted_candidates
                            and work_for(other_id, current_jobs) == 0
                        ),
                        None,
                    )
                    continue
                try:
                    selected_plan = recipe_builds.persist_plan_in_session(
                        session, prepared, now=clock()
                    )
                except (UnknownOutcomeError, RecipeImageAvailabilityError) as error:
                    code = str(getattr(error, "code", ""))
                    if code in _BUILDER_ADMISSION_CODES:
                        skipped[candidate_id] = (code, str(error)[:200])
                        attempted_candidates.add(candidate_id)
                        selected_candidate = next(
                            (
                                other_id
                                for other_id in candidate_ids
                                if other_id != candidate_id
                                and other_id not in attempted_candidates
                            ),
                            None,
                        )
                        continue
                    # The failed write is undone, not committed half done.
                    session.rollback()
                    return _planning_failure(error)
                builder_node_id = candidate_id
                if selected_plan is None:
                    session.rollback()
                    return BuildUnsettled(
                        RecipeImageCode.BUILD_UNAVAILABLE,
                        "selected Recipe build plan is unavailable",
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                build_input_sha256 = selected_plan.build_input_sha256
                try:
                    lock_build_dependency(
                        session,
                        recipe_revision_id=revision_id,
                        builder_node_id=candidate_id,
                        build_input_sha256=build_input_sha256,
                        build_id=selected_plan.build_id,
                    )
                except BuildConsumerError as error:
                    # The plan persisted above is not kept without its owner.
                    session.rollback()
                    return _dependency_unsettled(error)
                assigned_runtime = dict(parent_runtime)
                assigned_runtime["builder_node_id"] = candidate_id
                assigned_runtime["build_input_sha256"] = build_input_sha256
                parent.payload = dict(parent_payload) | {
                    "runtime": assigned_runtime,
                    "build_input_sha256": build_input_sha256,
                    "identity_key": build_input_sha256,
                    PREBUILT_PULL_KEY: pulls_prebuilt,
                }
                parent.updated_at = clock()
        if builder_node_id is None:
            # A resolved plan always carries the builder that produced it, so
            # without one the operation can only wait for capacity.
            return _capacity_wait(skipped, candidate_ids, prebuilt_unused)
        if selected_plan is None:
            try:
                prepared = recipe_builds.prepare_plan(
                    revision_id, builder_node_id, now=clock(), resolution=resolution
                )
            except (UnknownOutcomeError, RecipeImageAvailabilityError) as error:
                return _planning_failure(error)
            with sessions.begin() as session:
                service._require_claim(session, claim)
                try:
                    selected_plan = recipe_builds.persist_plan_in_session(
                        session, prepared, now=clock()
                    )
                except (UnknownOutcomeError, RecipeImageAvailabilityError) as error:
                    session.rollback()
                    return _planning_failure(error)
        if selected_plan is None:
            return BuildUnsettled(
                RecipeImageCode.BUILD_UNAVAILABLE,
                "reconstructed Recipe build plan is unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        plan = selected_plan
        if plan.build_input_sha256 != build_input_sha256:
            return BuildUnsettled(
                RecipeImageCode.IDENTITY_CONFLICT,
                "reconstructed build plan does not match the operation identity",
                reason=WaitReason.SCOPE_CHANGED,
            )
        # Publish the exact dependency before relying on the shared child.
        # This also re-enters ownership when a previously recorded image was
        # lost and its parent needs to prepare it again.
        with sessions.begin() as session:
            parent = service._require_claim(session, claim)
            try:
                lock_build_dependency(
                    session,
                    recipe_revision_id=revision_id,
                    builder_node_id=builder_node_id,
                    build_input_sha256=build_input_sha256,
                    build_id=plan.build_id,
                )
            except BuildConsumerError as error:
                return _dependency_unsettled(error)
            parent_payload = dict(parent.payload)
            stored_runtime = parent_payload.get("runtime")
            if not isinstance(stored_runtime, Mapping):
                return BuildUnsettled(
                    RecipeImageCode.OPERATION_INVALID,
                    "accepted availability runtime identity is invalid",
                    reason=WaitReason.STALE_PLAN,
                )
            parent_runtime = dict(stored_runtime)
            parent_runtime.update(
                builder_node_id=builder_node_id, build_input_sha256=build_input_sha256
            )
            parent_payload.update(
                runtime=parent_runtime,
                build_input_sha256=build_input_sha256,
                identity_key=build_input_sha256,
            )
            parent_payload.pop("image_result", None)
            stored_dependency = _build_dependency(parent_payload)
            if isinstance(stored_dependency, BuildUnsettled):
                session.rollback()
                return stored_dependency
            dependency = stored_dependency or _new_build_dependency(parent)
            parent_payload["build_dependency"] = dependency.model_dump(
                mode="json", exclude_none=True
            )
            parent.payload = parent_payload
            parent.updated_at = clock()
        build_request_id = str(dependency.request_key)

        def admission_guard(session: Session) -> None:
            parent = service._require_claim(session, claim)
            current = _build_dependency(parent.payload)
            runtime = require_mapping(parent.payload["runtime"], "runtime")
            if (
                not isinstance(current, RecipeBuildDependency)
                or str(current.request_key) != build_request_id
                or parent.payload.get("recipe_revision_id") != revision_id
                or parent.payload.get("build_input_sha256") != build_input_sha256
                or runtime.get("builder_node_id") != builder_node_id
            ):
                raise AvailabilityUnsettled(
                    RecipeImageCode.BUILD_INVALID,
                    "accepted build dependency changed",
                    reason=WaitReason.SCOPE_CHANGED,
                )

        try:
            operation = recipe_operations.build(
                plan,
                build_input_sha256=build_input_sha256,
                actor="recipe-image-availability",
                request_id=build_request_id,
                force=force,
                admission_guard=admission_guard,
            )
        except RecipeBuildAdmissionBusy as error:
            return BuildUnsettled(
                RecipeImageCode.BUILD_CAPACITY_WAIT,
                str(error),
                retryable=True,
                recovery_actions=("resume", "retry"),
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        except RecipeOperationConflict:
            recovered = recover_build(claim)
            if isinstance(recovered, BuildUnsettled):
                return recovered
            if recovered is None:
                raise
            operation = recipe_operations.get(recovered[0])
        with sessions.begin() as session:
            parent = service._require_claim(session, claim)
            current = _build_dependency(parent.payload)
            if isinstance(current, BuildUnsettled):
                return current
            if current is None or str(current.request_key) != build_request_id:
                return BuildUnsettled(
                    RecipeImageCode.BUILD_INVALID,
                    "accepted build dependency changed",
                    reason=WaitReason.SCOPE_CHANGED,
                )
            if (
                current.operation_id is not None
                and str(current.operation_id) != operation.id
            ):
                return BuildUnsettled(
                    RecipeImageCode.BUILD_INVALID,
                    "accepted build child changed",
                    reason=WaitReason.SCOPE_CHANGED,
                )
            current = current.model_copy(
                update={"operation_id": uuid.UUID(operation.id)}
            )
            parent.payload = dict(parent.payload) | {
                "build_dependency": current.model_dump(mode="json", exclude_none=True)
            }
        return _observe_build(
            sessions,
            operation,
            builder_node_id=builder_node_id,
            build_input_sha256=build_input_sha256,
            progress=progress,
            storage=storage,
        )

    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=reobserve_authority,
        transport=transport,
        builder=builder,
        clock=clock,
        model_cache=model_cache,
        max_parallel=max_parallel,
    )
    scheduler = None
    if with_scheduler:
        # The API process owns no image executor.  The worker asks the same
        # factory for a scheduler and dispatches its ``tick`` as a background
        # source, alongside model transfer and generic work.
        scheduler = RecipeImageAvailabilityScheduler(
            service,
            max_workers=max_parallel,
        )
    return RecipeImageAvailabilityProduction(service, scheduler, storage, transport)


async def run_availability_scheduler(
    scheduler: RecipeImageAvailabilityScheduler,
    stop: asyncio.Event,
    *,
    interval_seconds: float = 0.25,
) -> None:
    """Run scheduler ticks off the event loop until application shutdown."""

    if interval_seconds <= 0:
        raise InvalidValue(
            "availability scheduler interval must be positive",
            reason=InvalidRequestReason.OUT_OF_RANGE,
        )
    while not stop.is_set():
        await asyncio.to_thread(scheduler.tick)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


def _build_dependency(
    payload: Mapping[str, object],
) -> RecipeBuildDependency | BuildUnsettled | None:
    """The stored dependency, ``None`` when there is none, or the unknown that a
    malformed one is: the caller returns it and the claim runner asks again."""

    value = payload.get("build_dependency")
    if value is None:
        return None
    try:
        return read_stored_model(
            RecipeBuildDependency, json.dumps(value, allow_nan=False), from_json=True
        )
    except (TypeError, ValueError):
        return BuildUnsettled(
            RecipeImageCode.BUILD_INVALID,
            "accepted build dependency is malformed",
            reason=WaitReason.REPORT_UNCERTAIN,
        )


def _new_build_dependency(parent: Job) -> RecipeBuildDependency:
    # Store this before dispatch. Subsequent observation claims retain it;
    # a settled failed attempt or explicit new request gets a new identity.
    return RecipeBuildDependency(
        request_key=uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:recipe-image-build:{parent.id}:{parent.current_attempt}",
        )
    )


def _dependency_unsettled(error: BuildConsumerError) -> BuildUnsettled:
    """The shared build the operation depends on cannot be joined or kept."""

    return BuildUnsettled(
        error.code,
        str(error),
        reason=WaitReason.SCOPE_CHANGED,
        retryable=True,
        recovery_actions=(),
    )


def _planning_failure(error: Exception) -> BuildUnsettled:
    """Planning failed: a capacity wait if the builder is full, else unavailable."""

    code = str(getattr(error, "code", ""))
    reason = getattr(error, "typed_reason", None) or WaitReason.OBSERVATION_UNAVAILABLE
    if code in _BUILDER_ADMISSION_CODES:
        # Keep the builder's own reason visible: "full" and "no fresh inventory"
        # need different operator attention.
        blockers = [make_blocker(code, str(error)[:200])]
        detail = (
            "selected Recipe builder is currently unavailable or full "
            f"({code}: {str(error)[:200]})"
        )
        prebuilt_unused = getattr(error, "prebuilt_unused", None)
        if isinstance(prebuilt_unused, PrebuiltDecision):
            blockers.insert(0, _prebuilt_blocker(prebuilt_unused))
            detail = f"prebuilt image not used ({prebuilt_unused}); {detail}"
        return BuildUnsettled(
            RecipeImageCode.BUILD_CAPACITY_WAIT,
            detail[:512],
            reason=reason,
            retryable=True,
            recovery_actions=("resume", "retry"),
            blockers=tuple(blockers),
        )
    return BuildUnsettled(
        code or RecipeImageCode.BUILD_UNAVAILABLE,
        str(error)[:512],
        reason=reason,
        retryable=True,
    )


__all__ = [
    "RecipeImageAvailabilityProduction",
    "RecipeImageAvailabilityScheduler",
    "build_recipe_image_availability",
    "run_availability_scheduler",
]
