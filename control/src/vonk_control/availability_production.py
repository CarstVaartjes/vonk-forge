"""Production composition for model and recipe image availability.

The durable availability services deliberately keep their SQL state separate
from the process that dispatches work.  This module is the small production
boundary that supplies canonical recipe authority, verified OCI storage and
an executor which can be closed independently of the API event loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import RecipeDefinition

from .models import AgentNode, CatalogDocumentRevision, Job
from .recipe_image_availability import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
)
from .recipe_runtime_specs import compile_runtime_spec, resolve_recipe_entities
from .runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImageReceipt,
    SkopeoOCIImageTransport,
    persist_runtime_image_receipt,
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
            max_workers=max_workers,
            thread_name_prefix="vonk-recipe-image-availability",
        )
        self._owner_id = owner_id or f"availability-{uuid.uuid4().hex}"
        self._futures: set[Future[None]] = set()
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
            self._futures = {future for future in self._futures if not future.done()}
            capacity = self._max_workers - len(self._futures)
            if capacity <= 0:
                return 0
            claims = self._service.claim_pending(
                limit=capacity,
                owner_id=self._owner_id,
            )
            for claim in claims:
                self._futures.add(self._executor.submit(self._run, claim))
            return len(claims)

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
        revision_id: str,
    ) -> tuple[RecipeDefinition, Mapping[str, object]]:
        with sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.id == revision_id,
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
            package_handle: Mapping[str, object] | None = None
            builder_node_id: str | None = None
            if recipe.execution.mode == "build":
                candidates = tuple(
                    candidate
                    for candidate in session.scalars(
                        select(AgentNode)
                        .where(AgentNode.state == "active")
                        .order_by(AgentNode.id)
                    )
                    if candidate.architecture == "linux-arm64"
                    and "recipe.build.v1" in (candidate.capabilities or ())
                )
                plans: list[tuple[int, str, Any]] = []
                active_jobs = tuple(
                    session.scalars(
                        select(Job).where(
                            Job.state.in_({"queued", "running", "partial"}),
                            Job.kind.in_({"recipe.build.v1", "recipe.image.availability.v2"}),
                        )
                    )
                )
                for candidate in candidates:
                    try:
                        candidate_plan = recipe_builds.plan(
                            revision_id, candidate.id, now=clock()
                        )
                    except Exception:
                        continue
                    active_work = 0
                    for job in active_jobs:
                        if job.kind == "recipe.build.v1" and candidate.id in (job.targets or ()):
                            active_work += 1
                        elif job.kind == "recipe.image.availability.v2":
                            job_payload = job.payload if isinstance(job.payload, Mapping) else {}
                            job_runtime = job_payload.get("runtime")
                            if (
                                job_payload.get("builder_node_id") == candidate.id
                                or (
                                    isinstance(job_runtime, Mapping)
                                    and job_runtime.get("builder_node_id") == candidate.id
                                )
                            ):
                                active_work += 1
                    plans.append((active_work, candidate.id, candidate_plan))
                if not plans:
                    raise RecipeImageAvailabilityError(
                        "recipe_image.build_unavailable",
                        "no active compatible Recipe builder is available",
                    )
                _, builder_node_id, plan = min(plans, key=lambda item: (item[0], item[1]))
                package_handle = {"build_input_sha256": plan.build_input_sha256}
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
            result = dict(runtime) | {"recipe_revision_id": revision.id}
            if builder_node_id is not None:
                result["builder_node_id"] = builder_node_id
                assert package_handle is not None
                result["build_input_sha256"] = package_handle["build_input_sha256"]
            return recipe, result

    def builder(
        recipe: RecipeDefinition,
        runtime: Mapping[str, object],
        *,
        build_input_sha256: str,
        force: bool,
        progress: Callable[[Mapping[str, object]], None],
    ) -> Mapping[str, object]:
        del recipe
        revision_id = runtime.get("recipe_revision_id")
        builder_node_id = runtime.get("builder_node_id")
        if not isinstance(revision_id, str) or not isinstance(builder_node_id, str):
            raise RecipeImageAvailabilityError(
                "recipe_image.build_input_missing",
                "canonical build plan is unavailable after restart",
            )
        try:
            plan = recipe_builds.plan(revision_id, builder_node_id, now=clock())
        except Exception as error:
            raise RecipeImageAvailabilityError(
                str(getattr(error, "code", "recipe_image.build_unavailable")),
                str(error)[:512],
            ) from error
        if plan.build_input_sha256 != build_input_sha256:
            raise RecipeImageAvailabilityError(
                "recipe_image.identity_conflict",
                "reconstructed build plan does not match the operation identity",
            )
        operation = recipe_operations.build(
            plan,
            build_input_sha256=build_input_sha256,
            actor="recipe-image-availability",
            request_id=(
                str(uuid.uuid4())
                if force
                else str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"vonk:recipe-image-build:{build_input_sha256}",
                    )
                )
            ),
            force=force,
        )
        while operation.state not in {"succeeded", "failed", "expired"}:
            progress({"step": "build", "completed_bytes": 0})
            time.sleep(0.5)
            with sessions() as session:
                row = session.get(Job, operation.id)
                if row is None:
                    raise RecipeImageAvailabilityError(
                        "recipe_image.build_unavailable",
                        "durable build operation disappeared",
                        retryable=True,
                    )
                operation = type(operation)(
                    operation.id,
                    operation.kind,
                    operation.owner_id,
                    row.state,
                    operation.plan_digest,
                    operation.nodes,
                    dict(row.result) if isinstance(row.result, Mapping) else None,
                )
        if operation.state != "succeeded" or not isinstance(operation.result, Mapping):
            raise RecipeImageAvailabilityError(
                "recipe_image.build_failed",
                "canonical Recipe build failed",
                retryable=True,
                step="build",
            )
        return dict(operation.result) | {"build_id": operation.owner_id}

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
