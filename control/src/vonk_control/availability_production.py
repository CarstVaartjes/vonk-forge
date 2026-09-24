"""Production composition for model and recipe image availability.

The durable availability services deliberately keep their SQL state separate
from the process that dispatches work.  This module is the small production
boundary that supplies canonical recipe authority, verified OCI storage and
an executor which can be closed independently of the API event loop.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentFailureResult,
    RecipeBuildEvidence,
    canonical_message,
)
from vonk_agent_protocol.wire_model import OperationProgress
from vonk_forge_contracts import RecipeDefinition

from .admission_locking import (
    AdmissionLockBusy,
    acquire_admission_keys,
    node_admission_key,
)
from .bounded_json import require_mapping
from .models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
)
from .recipe_availability_intent import RecipeBuildDependency
from .recipe_build_cancellation import BuildConsumerError, lock_build_dependency
from .recipe_builds import (
    BUILD_ARTIFACT_FORMAT,
    RecipeBuildAdmissionBusy,
    RecipeBuildResolution,
)
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_build_plan,
    parse_stored_build_policy,
)
from .recipe_image_availability import (
    RecipeImageAvailabilityClaim,
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
)
from .recipe_operations import RecipeOperationConflict
from .recipe_runtime_specs import compile_runtime_spec, resolve_recipe_entities
from .recovery_policy import RecoveryDecision, classify, kind_for_agent_error
from .runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImageReceipt,
    SkopeoOCIImageTransport,
    persist_runtime_image_receipt,
)

_BUILDER_ADMISSION_CODES = frozenset(
    {
        "build.node_unknown",
        "build.node_incompatible",
        "build.capacity_busy",
        "build.inventory_missing",
        "build.inventory_stale",
        "build.capability_missing",
        "build.network_capability_missing",
        "build.insufficient_disk",
        "build.insufficient_memory",
        "build.runtime_changed",
    }
)


class RecipeImageAvailabilityScheduler:
    """Dispatch durable image claims on a bounded, independent executor."""

    def __init__(
        self,
        service: RecipeImageAvailabilityService,
        *,
        max_workers: int = 4,
        owner_id: str | None = None,
    ) -> None:
        if not 1 <= max_workers <= 16:
            raise ValueError("availability scheduler parallelism is invalid")
        self._service = service
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            # One coordination slot cannot consume the slots its image
            # children need. It admits/observes one child, then releases.
            max_workers=max_workers + 1,
            thread_name_prefix="vonk-recipe-image-availability",
        )
        self._owner_id = owner_id or f"availability-{uuid.uuid4().hex}"
        self._futures: set[Future[None]] = set()
        self._update_future: Future[None] | None = None
        self._lock = threading.Lock()
        self._closed = False

    @property
    def executor(self) -> ThreadPoolExecutor:
        """Expose the owned executor for lifecycle tests and diagnostics."""

        return self._executor

    def tick(self) -> int:
        """Claim and submit due work without waiting for image I/O or builds."""

        with self._lock:
            if self._closed:
                return 0
            self._service.reconcile_cancellations(limit=self._max_workers)
            self._service.advance_removals(limit=1)
            submitted = 0
            if self._update_future is None or self._update_future.done():
                update_claim = self._service.claim_update(self._owner_id)
                if update_claim is not None:
                    self._update_future = self._executor.submit(
                        self._service.run_update_claim, update_claim
                    )
                    submitted += 1
            self._futures = {future for future in self._futures if not future.done()}
            capacity = self._max_workers - len(self._futures)
            if capacity <= 0:
                return submitted
            claims = self._service.claim_pending(
                limit=capacity,
                owner_id=self._owner_id,
            )
            for claim in claims:
                self._futures.add(self._executor.submit(self._run, claim))
            return len(claims) + submitted

    def _run(self, claim: Any) -> None:
        self._service.run_claim(claim)

    def close(self) -> None:
        """Stop dispatch and release threads; durable claims remain restartable."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


@dataclass(frozen=True, slots=True)
class RecipeImageAvailabilityProduction:
    """Concrete production services and their independently owned scheduler."""

    service: RecipeImageAvailabilityService
    scheduler: RecipeImageAvailabilityScheduler | None
    storage: FilesystemRuntimeImageStorage
    transport: SkopeoOCIImageTransport

    def close(self) -> None:
        if self.scheduler is not None:
            self.scheduler.close()


def build_recipe_image_availability(
    sessions: sessionmaker[Session],
    *,
    settings: Any | None = None,
    artifact_root: Any | None = None,
    managed_catalog_sync: Any | None,
    recipe_builds: Any,
    recipe_operations: Any,
    model_cache: Any | None = None,
    clock: Callable[[], datetime],
    max_parallel: int = 4,
    max_parallel_builds: int = 1,
    with_scheduler: bool = False,
) -> RecipeImageAvailabilityProduction:
    """Compose canonical catalog resolution, OCI storage, and image execution.

    Catalog refresh is owned by the production app's automatic sync task.
    Each request resolves its selected immutable revision from SQL, so a
    refresh can advance the global head without changing an operation's
    identity.  The optional scheduler only claims durable operations.
    """

    image_root = artifact_root or getattr(settings, "agent_artifact_root", None)
    if image_root is None:
        raise ValueError("recipe image artifact root is required")
    storage = FilesystemRuntimeImageStorage(image_root)
    transport = SkopeoOCIImageTransport()

    def authority(
        recipe_revision_id: str,
        *,
        force: bool = False,
    ) -> tuple[RecipeDefinition, Mapping[str, object]]:
        with sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.id == recipe_revision_id,
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
            )
            if revision is None:
                raise RecipeImageAvailabilityError(
                    "recipe_image.recipe_unavailable",
                    "selected recipe revision is unavailable or inactive",
                )
            try:
                recipe = RecipeDefinition.model_validate(revision.document)
                entities = resolve_recipe_entities(session, revision.document)
            except Exception as error:
                raise RecipeImageAvailabilityError(
                    "recipe_image.recipe_invalid",
                    "selected recipe is not a canonical RecipeDefinition",
                ) from error
            resolved_revision_id = revision.id
            # Build resolution consults managed storage. Close the read
            # transaction first: a transaction contains database work only.
            session.close()
            package_handle: Mapping[str, object] | None = None
            builder_node_id: str | None = None
            if recipe.execution.mode == "build":
                resolution = None
                resolve = getattr(recipe_builds, "resolve", None)
                if isinstance(resolve, Callable):
                    try:
                        resolution = resolve(recipe_revision_id)
                    except Exception as error:
                        raise RecipeImageAvailabilityError(
                            str(
                                getattr(error, "code", "recipe_image.build_unavailable")
                            ),
                            str(error)[:512],
                            retryable=True,
                            recovery_actions=("retry",),
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
                        # Builder selection and final input binding happen only
                        # at dispatch. Persist the immutable intent so
                        # saturation can queue a durable parent operation.
                        package_handle = {
                            "input_intent_sha256": resolution.input_intent_sha256,
                            # A source-build operation has no final image until
                            # dispatch. Compile a provisional runtime identity
                            # so the durable parent can queue without a builder;
                            # the verified build receipt replaces it on success.
                            "image_digest": resolution.input_intent_sha256,
                            "image_reference": (
                                "localhost/vonk/recipe-build@sha256:"
                                f"{resolution.input_intent_sha256}"
                            ),
                        }
            try:
                runtime = _compile_consistent_runtime(
                    recipe,
                    resolved_entities=entities,
                    package_handle=package_handle,
                )
            except RecipeImageAvailabilityError:
                raise
            except Exception as error:
                raise RecipeImageAvailabilityError(
                    "recipe_image.runtime_invalid",
                    "compiled runtime projection is unavailable",
                ) from error
            # These fields are Controller scheduler metadata.  They are
            # persisted with the operation so a restart can reconstruct a
            # source-build plan instead of relying on process memory.
            result = dict(runtime) | {"recipe_revision_id": resolved_revision_id}
            if builder_node_id is not None:
                result["builder_node_id"] = builder_node_id
            if package_handle is not None and isinstance(
                package_handle.get("build_input_sha256"), str
            ):
                result["build_input_sha256"] = package_handle["build_input_sha256"]
            if package_handle is not None and isinstance(
                package_handle.get("input_intent_sha256"), str
            ):
                result["input_intent_sha256"] = package_handle["input_intent_sha256"]
            return recipe, result

    def recover_build(
        claim: RecipeImageAvailabilityClaim,
        *,
        join_active: bool = True,
        resolution: RecipeBuildResolution | None = None,
    ) -> tuple[str, str, str] | None:
        """Recover committed work before source, inventory or capacity planning."""

        with sessions.begin() as session:
            parent = service._require_claim(session, claim)
            payload = parent.payload
            runtime = payload.get("runtime")
            if not isinstance(runtime, Mapping):
                raise RecipeImageAvailabilityError(
                    "recipe_image.operation_invalid",
                    "accepted build runtime is invalid",
                )
            dependency = _build_dependency(payload)
            child = None
            if dependency is not None:
                child = (
                    session.get(Job, str(dependency.operation_id))
                    if dependency.operation_id is not None
                    else session.scalar(
                        select(Job).where(Job.request_id == str(dependency.request_key))
                    )
                )
                if dependency.operation_id is not None and child is None:
                    raise RecipeImageAvailabilityError(
                        "recipe_image.build_invalid", "accepted build child is missing"
                    )
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
                    raise RecipeImageAvailabilityError(
                        "recipe_image.identity_conflict",
                        "accepted build intent no longer matches its resolution",
                    )
                # Selecting new capacity would count an accepted build's own
                # reservation against its consumers. Resolve an active exact
                # execution first, then join under the same cancellation fence
                # used by consumers that already know their builder.
                candidates = session.scalars(
                    select(RecipeBuild)
                    .join(Job, Job.payload["owner_id"].as_string() == RecipeBuild.id)
                    .where(
                        RecipeBuild.recipe_revision_id == revision_id,
                        Job.kind == "recipe.build.v1",
                        Job.state.in_(("queued", "running")),
                    )
                    .order_by(RecipeBuild.created_at, RecipeBuild.id)
                ).unique()
                for candidate in candidates:
                    try:
                        policy = parse_stored_build_policy(candidate.policy_report)
                        request = parse_stored_build_plan(candidate.plan)
                    except RecipeExecutionContractError as error:
                        raise RecipeImageAvailabilityError(
                            "recipe_image.build_invalid",
                            "accepted shared build evidence is invalid",
                        ) from error
                    if policy.builder_binary_digest is None:
                        raise RecipeImageAvailabilityError(
                            "recipe_image.build_invalid",
                            "accepted shared build has no recorded builder identity",
                        )
                    if (
                        policy.artifact_format != BUILD_ARTIFACT_FORMAT
                        or policy.source_bundle_sha256
                        != resolution.source_bundle_sha256
                        or candidate.source_bundle_sha256
                        != resolution.source_bundle_sha256
                        or candidate.build_input_sha256
                        != resolution.build_input_for_builder(
                            policy.builder_binary_digest
                        )
                    ):
                        continue
                    if (
                        str(request.build_id) != candidate.id
                        or request.build_input_sha256 != candidate.build_input_sha256
                        or str(request.recipe_revision_id) != revision_id
                        or request.recipe_content_sha256
                        != resolution.recipe_content_sha256
                    ):
                        raise RecipeImageAvailabilityError(
                            "recipe_image.build_invalid",
                            "accepted shared build request identity changed",
                        )
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
                    raise _build_dependency_error(error) from error
                if build is not None:
                    child = session.scalar(
                        select(Job)
                        .where(
                            Job.kind == "recipe.build.v1",
                            Job.payload["owner_id"].as_string() == build.id,
                            Job.payload["plan_digest"].as_string() == digest,
                            Job.state.in_(("queued", "running")),
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
                or owner.recipe_revision_id != revision_id
                or owner.builder_node_id != builder_id
                or owner.build_input_sha256 != digest
                or child.payload.get("plan_digest") != digest
                or child.targets != [builder_id]
                or not isinstance(builder_id, str)
                or not isinstance(digest, str)
            ):
                raise RecipeImageAvailabilityError(
                    "recipe_image.build_invalid",
                    "accepted build child identity changed",
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
    ) -> Mapping[str, object]:
        del recipe
        recovered = recover_build(claim, join_active=False)
        if recovered is not None:
            child_id, child_builder, child_input = recovered
            return _observe_build(
                sessions,
                recipe_operations.get(child_id),
                builder_node_id=child_builder,
                build_input_sha256=child_input,
                progress=progress,
            )
        revision_id = runtime.get("recipe_revision_id")
        raw_builder_node_id = runtime.get("builder_node_id")
        builder_node_id: str | None = (
            raw_builder_node_id if isinstance(raw_builder_node_id, str) else None
        )
        if not isinstance(revision_id, str):
            raise RecipeImageAvailabilityError(
                "recipe_image.build_input_missing",
                "canonical build plan is unavailable after restart",
            )
        resolution = recipe_builds.resolve(revision_id)
        cached = None if force else _cached_build_receipt(resolution)
        if cached is not None and build_input_sha256 in {
            "",
            cached["build_input_sha256"],
        }:
            return cached
        recovered = recover_build(claim, resolution=resolution)
        if recovered is not None:
            child_id, child_builder, child_input = recovered
            return _observe_build(
                sessions,
                recipe_operations.get(child_id),
                builder_node_id=child_builder,
                build_input_sha256=child_input,
                progress=progress,
            )
        # SQL may still record a succeeded build whose archive is gone. The
        # resolution reports that as stale cache loss, and dispatch must build
        # again instead of replaying the vanished result.
        force = force or bool(getattr(resolution, "stale_receipt", False))
        selected_plan: Any | None = None
        selected_candidate: str | None = None
        candidate_ids: tuple[str, ...] = ()
        attempted_candidates: set[str] = set()
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
                            .where(AgentNode.state == "active")
                            .order_by(AgentNode.node_id)
                        )
                        if candidate.architecture == "linux-arm64"
                        and "recipe.build.v1" in (candidate.capabilities or ())
                    )
                    candidate_ids = tuple(candidate.node_id for candidate in candidates)
                    active_jobs = tuple(
                        session.scalars(
                            select(Job).where(
                                Job.state.in_({"queued", "running", "partial"}),
                                Job.kind.in_(
                                    {"recipe.build.v1", "recipe.image.availability.v2"}
                                ),
                            )
                        )
                    )

                    def work_for(node_id: str, jobs: tuple[Job, ...]) -> int:
                        return sum(
                            1
                            for job in jobs
                            if (
                                job.kind == "recipe.build.v1"
                                and node_id in (job.targets or ())
                            )
                            or (
                                job.kind == "recipe.image.availability.v2"
                                and isinstance(job.payload, Mapping)
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

                    ordered_candidates = sorted(
                        (work_for(candidate.node_id, active_jobs), candidate.node_id)
                        for candidate in candidates
                    )
                    for _, candidate_id in ordered_candidates:
                        try:
                            acquire_admission_keys(
                                session, (node_admission_key(candidate_id),)
                            )
                        except AdmissionLockBusy:
                            continue
                        locked = session.scalar(
                            select(AgentNode)
                            .where(AgentNode.node_id == candidate_id)
                            .with_for_update(skip_locked=True)
                        )
                        if locked is None:
                            continue
                        current_jobs = tuple(
                            session.scalars(
                                select(Job).where(
                                    Job.state.in_({"queued", "running", "partial"}),
                                    Job.kind.in_(
                                        {
                                            "recipe.build.v1",
                                            "recipe.image.availability.v2",
                                        }
                                    ),
                                )
                            )
                        )
                        if current_jobs and work_for(candidate_id, current_jobs) > min(
                            work_for(candidate.node_id, current_jobs)
                            for candidate in candidates
                        ):
                            continue
                        selected_candidate = candidate_id
                        break
                    if selected_candidate is None:
                        raise RecipeImageAvailabilityError(
                            "recipe_image.build_capacity_wait",
                            "no compatible Recipe builder is currently available",
                            retryable=True,
                            recovery_actions=("resume", "retry"),
                        )
        while selected_plan is None and selected_candidate is not None:
            candidate_id = selected_candidate
            try:
                prepared = recipe_builds.prepare_plan(
                    revision_id,
                    candidate_id,
                    now=clock(),
                    resolution=resolution,
                )
            except Exception as error:
                code = str(getattr(error, "code", ""))
                if code in _BUILDER_ADMISSION_CODES:
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
                raise RecipeImageAvailabilityError(
                    code or "recipe_image.build_unavailable",
                    str(error)[:512],
                ) from error
            with sessions.begin() as session:
                try:
                    acquire_admission_keys(session, (node_admission_key(candidate_id),))
                except AdmissionLockBusy:
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
                            Job.state.in_({"queued", "running", "partial"}),
                            Job.kind.in_(
                                {"recipe.build.v1", "recipe.image.availability.v2"}
                            ),
                        )
                    )
                )
                if current_jobs and work_for(candidate_id, current_jobs) > min(
                    work_for(other_id, current_jobs) for other_id in candidate_ids
                ):
                    attempted_candidates.add(candidate_id)
                    selected_candidate = next(
                        (
                            other_id
                            for other_id in candidate_ids
                            if other_id != candidate_id
                            and other_id not in attempted_candidates
                            and work_for(other_id, current_jobs)
                            == min(
                                work_for(item_id, current_jobs)
                                for item_id in candidate_ids
                            )
                        ),
                        None,
                    )
                    continue
                try:
                    selected_plan = recipe_builds.persist_plan_in_session(
                        session, prepared, now=clock()
                    )
                except Exception as error:
                    code = str(getattr(error, "code", ""))
                    if code in _BUILDER_ADMISSION_CODES:
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
                    raise RecipeImageAvailabilityError(
                        code or "recipe_image.build_unavailable",
                        str(error)[:512],
                    ) from error
                builder_node_id = candidate_id
                if selected_plan is None:
                    raise RecipeImageAvailabilityError(
                        "recipe_image.build_unavailable",
                        "selected Recipe build plan is unavailable",
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
                    raise RecipeImageAvailabilityError(
                        error.code,
                        str(error),
                        retryable=error.retryable,
                        recovery_actions=("retry",) if error.retryable else (),
                    ) from error
                assigned_runtime = dict(parent_runtime)
                assigned_runtime["builder_node_id"] = candidate_id
                assigned_runtime["build_input_sha256"] = build_input_sha256
                parent.payload = dict(parent_payload) | {
                    "runtime": assigned_runtime,
                    "build_input_sha256": build_input_sha256,
                    "identity_key": build_input_sha256,
                }
                parent.updated_at = clock()
        if builder_node_id is None:
            # A resolved plan always carries the builder that produced it, so
            # without one the operation can only wait for capacity.
            raise RecipeImageAvailabilityError(
                "recipe_image.build_capacity_wait",
                "no compatible Recipe builder is currently available",
                retryable=True,
                recovery_actions=("resume", "retry"),
            )
        if selected_plan is None:
            try:
                prepared = recipe_builds.prepare_plan(
                    revision_id, builder_node_id, now=clock(), resolution=resolution
                )
            except Exception as error:
                raise _build_planning_error(error) from error
            with sessions.begin() as session:
                service._require_claim(session, claim)
                try:
                    selected_plan = recipe_builds.persist_plan_in_session(
                        session, prepared, now=clock()
                    )
                except Exception as error:
                    raise _build_planning_error(error) from error
        if selected_plan is None:
            raise RecipeImageAvailabilityError(
                "recipe_image.build_unavailable",
                "reconstructed Recipe build plan is unavailable",
            )
        plan = selected_plan
        if plan.build_input_sha256 != build_input_sha256:
            raise RecipeImageAvailabilityError(
                "recipe_image.identity_conflict",
                "reconstructed build plan does not match the operation identity",
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
                raise RecipeImageAvailabilityError(
                    error.code,
                    str(error),
                    retryable=error.retryable,
                    recovery_actions=("retry",) if error.retryable else (),
                ) from error
            parent_payload = dict(parent.payload)
            stored_runtime = parent_payload.get("runtime")
            if not isinstance(stored_runtime, Mapping):
                raise RecipeImageAvailabilityError(
                    "recipe_image.operation_invalid",
                    "accepted availability runtime identity is invalid",
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
            dependency = _build_dependency(parent_payload) or _new_build_dependency(
                parent
            )
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
                current is None
                or str(current.request_key) != build_request_id
                or parent.payload.get("recipe_revision_id") != revision_id
                or parent.payload.get("build_input_sha256") != build_input_sha256
                or runtime.get("builder_node_id") != builder_node_id
            ):
                raise RecipeImageAvailabilityError(
                    "recipe_image.build_invalid", "accepted build dependency changed"
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
            raise RecipeImageAvailabilityError(
                "recipe_image.build_capacity_wait",
                str(error),
                retryable=True,
                recovery_actions=("resume", "retry"),
            ) from error
        except RecipeOperationConflict:
            recovered = recover_build(claim)
            if recovered is None:
                raise
            operation = recipe_operations.get(recovered[0])
        with sessions.begin() as session:
            parent = service._require_claim(session, claim)
            current = _build_dependency(parent.payload)
            if current is None or str(current.request_key) != build_request_id:
                raise RecipeImageAvailabilityError(
                    "recipe_image.build_invalid", "accepted build dependency changed"
                )
            if (
                current.operation_id is not None
                and str(current.operation_id) != operation.id
            ):
                raise RecipeImageAvailabilityError(
                    "recipe_image.build_invalid", "accepted build child changed"
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
        )

    def receipt_writer(
        session: Session,
        recipe_revision_id: str,
        content_digest: str,
        execution_key: str,
        receipt: RuntimeImageReceipt,
    ) -> None:
        persist_runtime_image_receipt(
            session,
            recipe_revision_id=recipe_revision_id,
            original_content_digest=content_digest,
            effective_execution_key=execution_key,
            receipt=receipt,
            verified_at=clock(),
        )

    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=authority,
        transport=transport,
        builder=builder,
        clock=clock,
        receipt_writer=receipt_writer,
        model_cache=model_cache,
        max_parallel=max_parallel,
        max_parallel_builds=max_parallel_builds,
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
        raise ValueError("availability scheduler interval must be positive")
    while not stop.is_set():
        await asyncio.to_thread(scheduler.tick)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


def _build_dependency(payload: Mapping[str, object]) -> RecipeBuildDependency | None:
    value = payload.get("build_dependency")
    if value is None:
        return None
    try:
        return RecipeBuildDependency.model_validate_json(
            json.dumps(value, allow_nan=False)
        )
    except (TypeError, ValueError) as error:
        raise RecipeImageAvailabilityError(
            "recipe_image.build_invalid", "accepted build dependency is malformed"
        ) from error


def _new_build_dependency(parent: Job) -> RecipeBuildDependency:
    # Store this before dispatch. Subsequent observation claims retain it;
    # a settled failed attempt or explicit new request gets a new identity.
    return RecipeBuildDependency(
        request_key=uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:recipe-image-build:{parent.id}:{parent.current_attempt}",
        )
    )


def _build_dependency_error(error: BuildConsumerError) -> RecipeImageAvailabilityError:
    return RecipeImageAvailabilityError(
        error.code,
        str(error),
        retryable=error.retryable,
        recovery_actions=("retry",) if error.retryable else (),
    )


def _build_planning_error(error: Exception) -> RecipeImageAvailabilityError:
    code = str(getattr(error, "code", ""))
    if code in _BUILDER_ADMISSION_CODES:
        return RecipeImageAvailabilityError(
            "recipe_image.build_capacity_wait",
            "selected Recipe builder is currently unavailable or full",
            retryable=True,
            recovery_actions=("resume", "retry"),
        )
    return RecipeImageAvailabilityError(
        code or "recipe_image.build_unavailable", str(error)[:512]
    )


def _observe_build(
    sessions: sessionmaker[Session],
    operation: Any,
    *,
    builder_node_id: str,
    build_input_sha256: str,
    progress: Callable[[Mapping[str, object]], None],
) -> Mapping[str, object]:
    if operation.state == "cancelled":
        raise RecipeImageAvailabilityError(
            "recipe_image.build_cancelled",
            "accepted build was cancelled",
            recovery_actions=("force_rebuild",),
        )
    if operation.state in {"queued", "running", "waiting-for-operator"}:
        with sessions() as session:
            current_progress = _build_progress(session, operation.id, builder_node_id)
        if current_progress is not None:
            progress(current_progress.model_dump(mode="json", exclude_none=True))
        raise RecipeImageAvailabilityError(
            "recipe_image.build_wait",
            f"waiting for build {operation.id} on {builder_node_id}: {operation.state}",
            retryable=True,
            retry_after_seconds=5,
        )
    if operation.state in {"failed", "expired"}:
        aggregate = operation.result
        node_evidence = (
            aggregate.get("node_evidence") if isinstance(aggregate, Mapping) else None
        )
        raw_failure = (
            node_evidence.get(builder_node_id)
            if isinstance(node_evidence, Mapping)
            else None
        )
        if not isinstance(raw_failure, Mapping):
            raise RecipeImageAvailabilityError(
                "recipe_image.build_invalid",
                "canonical Recipe build failure evidence is missing or invalid",
                step="build",
            )
        try:
            failure = AgentFailureResult.model_validate_json(
                canonical_message(raw_failure)
            )
        except (TypeError, ValueError) as error:
            raise RecipeImageAvailabilityError(
                "recipe_image.build_invalid",
                "canonical Recipe build failure evidence is missing or invalid",
                step="build",
            ) from error
        failure_document = failure.model_dump(mode="json", exclude_none=True)
        retryable = (
            classify(kind_for_agent_error(failure_document)) is RecoveryDecision.RETRY
        )
        summary = failure.summary or failure.reason or "canonical Recipe build failed"
        category = (
            failure.diagnostics.category if failure.diagnostics is not None else None
        )
        detail = f"{category}: {summary}" if category is not None else summary
        diagnostic = failure.diagnostic
        if failure.diagnostics is not None:
            diagnostic = (
                failure.diagnostics.stderr.text
                or failure.diagnostics.stdout.text
                or diagnostic
            )
        raise RecipeImageAvailabilityError(
            failure.error_code or "recipe_image.build_failed",
            detail,
            retryable=retryable,
            retry_after_seconds=(failure.retry_after_seconds if retryable else None),
            log_excerpt=diagnostic or None,
            step=failure.stage or "build",
        )
    if operation.state != "succeeded" or not isinstance(operation.result, Mapping):
        raise RecipeImageAvailabilityError(
            "recipe_image.build_invalid", "canonical Recipe build outcome is invalid"
        )
    aggregate = operation.result
    successful_nodes = aggregate.get("successful_nodes")
    node_evidence = aggregate.get("node_evidence")
    raw_evidence = (
        node_evidence.get(builder_node_id)
        if isinstance(node_evidence, Mapping)
        else None
    )
    if (
        not isinstance(successful_nodes, list)
        or builder_node_id not in successful_nodes
        or not isinstance(raw_evidence, Mapping)
    ):
        raise RecipeImageAvailabilityError(
            "recipe_image.build_invalid",
            "canonical Recipe build evidence is incomplete",
        )
    try:
        evidence = RecipeBuildEvidence.model_validate_json(
            json.dumps(dict(raw_evidence))
        )
    except (TypeError, ValueError) as error:
        raise RecipeImageAvailabilityError(
            "recipe_image.build_invalid", "canonical Recipe build evidence is invalid"
        ) from error
    if evidence.build_input_sha256 != build_input_sha256:
        raise RecipeImageAvailabilityError(
            "recipe_image.identity_conflict",
            "canonical Recipe build evidence does not match its inputs",
        )
    return evidence.model_dump(mode="json") | {
        "state": operation.state,
        "build_id": operation.owner_id,
        "build_input_sha256": build_input_sha256,
        "builder_node_id": builder_node_id,
    }


def _cached_build_receipt(resolution: Any) -> Mapping[str, object] | None:
    """Shape one prepared-build resolution into the canonical build receipt.

    This is the only source-build reuse test. The resolution proves the exact
    archive is present on disk; when its verification receipt also exists the
    values come from that receipt, and when the receipt is missing or
    incomplete the values come from the SQL build candidate so preparation
    re-verifies the bytes and republishes the receipt instead of building
    again. Queue-time and dispatch-time callers share this one answer.
    """

    if getattr(resolution, "cached", False) is not True:
        return None
    identity = (
        resolution.build_id,
        resolution.builder_node_id,
        resolution.build_input_sha256,
        resolution.image_digest,
        resolution.oci_layout_sha256,
    )
    if (
        not all(isinstance(value, str) for value in identity)
        or not isinstance(resolution.image_bytes, int)
        or isinstance(resolution.image_bytes, bool)
        or resolution.image_bytes < 1
    ):
        raise RecipeImageAvailabilityError(
            "recipe_image.build_invalid",
            "cached Recipe build receipt is incomplete",
        )
    return {
        "state": "succeeded",
        "build_id": resolution.build_id,
        "builder_node_id": resolution.builder_node_id,
        "build_input_sha256": resolution.build_input_sha256,
        "image_digest": resolution.image_digest,
        "oci_layout_sha256": resolution.oci_layout_sha256,
        "image_bytes": resolution.image_bytes,
    }


__all__ = [
    "RecipeImageAvailabilityProduction",
    "RecipeImageAvailabilityScheduler",
    "build_recipe_image_availability",
    "run_availability_scheduler",
]


def _compile_consistent_runtime(
    recipe: RecipeDefinition,
    *,
    resolved_entities: Mapping[str, object],
    package_handle: object,
) -> Mapping[str, object]:
    """Compile every canonical role/rank and require one image identity."""

    roles = tuple(recipe.topology.roles)
    if not roles:
        raise RecipeImageAvailabilityError(
            "recipe_image.runtime_invalid", "canonical recipe has no topology roles"
        )
    compiled: list[Mapping[str, object]] = []
    first_rank = 0
    for role in roles:
        role_count = int(role.count)
        for rank in range(first_rank, first_rank + role_count):
            projection = compile_runtime_spec(
                recipe,
                resolved_entities=resolved_entities,
                role=role.name,
                rank=rank,
                package_handle=package_handle,
            )
            runtime = projection.get("runtime")
            if not isinstance(runtime, Mapping):
                raise RecipeImageAvailabilityError(
                    "recipe_image.runtime_invalid",
                    "compiled runtime projection is unavailable",
                )
            compiled.append(runtime)
        first_rank += role_count
    first = compiled[0]
    identity = tuple(first.get(key) for key in ("image", "architecture", "interface"))
    if any(
        tuple(runtime.get(key) for key in ("image", "architecture", "interface"))
        != identity
        for runtime in compiled[1:]
    ):
        raise RecipeImageAvailabilityError(
            "recipe_image.runtime_invalid",
            "canonical recipe roles do not share one runtime image identity",
        )
    return first


def _build_progress(
    session: Session, job_id: str, node_id: str
) -> OperationProgress | None:
    """Read the current attempt's typed heartbeat, including image uploads."""
    document = session.scalar(
        select(AgentOperationAttempt.progress)
        .join(
            AgentOperation,
            AgentOperation.id == AgentOperationAttempt.operation_id,
        )
        .where(
            AgentOperation.parent_job_id == job_id,
            AgentOperation.node_id == node_id,
            AgentOperationAttempt.attempt == AgentOperation.current_attempt,
        )
    )
    if document is None:
        return None
    return OperationProgress.model_validate_json(json.dumps(document))
