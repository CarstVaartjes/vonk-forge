"""Durable job worker entry point and bounded handler registry."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import DBAPIError, OperationalError
from vonk_agent_protocol import UnknownOutcomeError
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)

from .bounded_retry import bounded_attempts
from .capabilities import CapabilityRegistry
from .capability_contract import ControllerCapability
from .jobs import JobService
from .logging import log_event, redact_text
from .resource_planning import PLATFORM_MEMORY_FLOOR_BYTES
from .worker_memory_contract import WorkerMemoryComponent

_PROCESS_INSTANCE = re.compile(r"[0-9a-f]{64}\Z")

#: Operational failures a durable source or handler can recover from on the
#: next pass.  Programming defects (``AssertionError`` and friends) stay
#: uncaught so they are never mistaken for a retryable dependency failure.
#: A bounded PostgreSQL wait (lock or statement timeout) surfaces as
#: ``OperationalError``/``DBAPIError``; containing it here keeps the loop
#: heartbeat running and the failed source retried on a later pass instead of
#: turning database contention into a worker tick failure.
_SOURCE_FAILURES = (
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    KeyError,
    OperationalError,
    DBAPIError,
)

_LOGGER = logging.getLogger("vonk-control-worker")
_WORKER_WATCHDOG_TIMEOUT_SECONDS = 180


class WorkerWatchdog:
    """Track completed scheduler loops and detect a stalled main thread."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("worker watchdog timeout must be positive")
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._last_completed = clock()

    def beat(self) -> None:
        with self._lock:
            self._last_completed = self._clock()

    def stalled(self) -> bool:
        with self._lock:
            return self._clock() - self._last_completed > self._timeout_seconds


def current_worker_instance_id(proc_root: Path = Path("/proc"), pid: int = 1) -> str:
    """Identify one Linux process lifetime without persistent mutable state."""
    boot_id = (proc_root / "sys/kernel/random/boot_id").read_text().strip()
    process_stat = (proc_root / str(pid) / "stat").read_text().strip()
    closing_parenthesis = process_stat.rfind(")")
    fields_after_name = process_stat[closing_parenthesis + 2 :].split()
    if closing_parenthesis < 1 or len(fields_after_name) < 20:
        raise RuntimeError("worker process identity is unavailable")
    start_ticks = fields_after_name[19]
    pid_namespace = (proc_root / str(pid) / "ns/pid").stat().st_ino
    material = f"{boot_id}\n{pid}\n{pid_namespace}\n{start_ticks}\n".encode()
    return hashlib.sha256(material).hexdigest()


class WorkerHeartbeatRecorder:
    """Persist readiness for the currently running scheduler process."""

    def __init__(
        self,
        sessions: Any,
        *,
        process_instance_id: str,
        clock: Callable[[], datetime],
    ) -> None:
        if _PROCESS_INSTANCE.fullmatch(process_instance_id) is None:
            raise ValueError("worker process instance ID is invalid")
        self._sessions = sessions
        self._process_instance_id = process_instance_id
        self._clock = clock
        self._register_process_start()

    def _register_process_start(self) -> None:
        from sqlalchemy import select

        from cluster_profiles.runtime_identity import packaged_runtime_identity

        from .models import ControlProcessHeartbeat

        identity = packaged_runtime_identity()

        with self._sessions.begin() as session:
            heartbeat = session.scalar(
                select(ControlProcessHeartbeat)
                .where(ControlProcessHeartbeat.process_kind == "worker")
                .where(
                    ControlProcessHeartbeat.process_instance_id
                    == self._process_instance_id
                )
                .with_for_update()
            )
            if heartbeat is None:
                session.add(
                    ControlProcessHeartbeat(
                        process_kind="worker",
                        process_instance_id=self._process_instance_id,
                        source_sha=identity.source_sha,
                        worker_contract_sha256=identity.worker_contract_sha256,
                        loop_sequence=0,
                        completed_at=None,
                    )
                )
                return
            heartbeat.process_instance_id = self._process_instance_id
            heartbeat.source_sha = identity.source_sha
            heartbeat.worker_contract_sha256 = identity.worker_contract_sha256
            heartbeat.loop_sequence = 0
            heartbeat.completed_at = None

    def completed_loop(self) -> None:
        from sqlalchemy import select

        from .models import ControlProcessHeartbeat

        completed_at = self._clock()
        if (
            not isinstance(completed_at, datetime)
            or completed_at.tzinfo is None
            or completed_at.utcoffset() is None
        ):
            raise ValueError("worker heartbeat clock must be timezone-aware")
        with self._sessions.begin() as session:
            heartbeat = session.scalar(
                select(ControlProcessHeartbeat)
                .where(ControlProcessHeartbeat.process_kind == "worker")
                .where(
                    ControlProcessHeartbeat.process_instance_id
                    == self._process_instance_id
                )
                .with_for_update()
            )
            if (
                heartbeat is None
                or heartbeat.process_instance_id != self._process_instance_id
            ):
                raise RuntimeError("worker process instance changed")
            heartbeat.loop_sequence += 1
            heartbeat.completed_at = completed_at.astimezone(UTC)


@dataclass(frozen=True)
class HandlerRequest(Mapping[str, object]):
    job_id: str
    kind: str
    payload: Mapping[str, object]
    authority_revision: str
    targets: tuple[str, ...]

    def __getitem__(self, key: str) -> object:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)


Handler = Callable[[HandlerRequest], Mapping[str, object]]


class Worker:
    def __init__(
        self,
        jobs: JobService,
        worker_id: str,
        handlers: Mapping[str, Handler],
        *,
        housekeeping: Callable[[], object] | None = None,
        artifact_housekeeping: Callable[[], object] | None = None,
        recipes=None,
        model_cache: object | None = None,
        background_services: Sequence[Callable[[], object]] = (),
        background_closers: Sequence[Callable[[], object]] = (),
        loop_heartbeat: Callable[[], object] | None = None,
        memory_sources: Sequence[object] = (),
    ) -> None:
        self._jobs = jobs
        self._worker_id = worker_id
        self._handlers = dict(handlers)
        self._housekeeping = housekeeping
        self._artifact_housekeeping = artifact_housekeeping
        self._recipes = recipes
        self._model_cache = model_cache
        self._background_services = tuple(background_services)
        self._background_closers = tuple(background_closers)
        self._loop_heartbeat = loop_heartbeat
        self._memory_sources = tuple(memory_sources)
        self._source_cursor = 0
        self._closed = False

    def close(self) -> None:
        """Close worker-owned executors while leaving durable work resumable."""

        if self._closed:
            return
        self._closed = True
        closers = list(self._background_closers)
        if self._model_cache is not None:
            close = getattr(self._model_cache, "close", None)
            if callable(close):
                closers.append(close)
        for closer in closers:
            for _attempt in bounded_attempts():
                try:
                    closer()
                    break
                except UnknownOutcomeError as error:
                    log_event(
                        _LOGGER,
                        "worker.shutdown_checkpoint_deferred",
                        service="control-worker",
                        message=redact_text(error),
                        resume="next shutdown attempt or Controller restart",
                    )

    def memory_footprint(self) -> dict[WorkerMemoryComponent, int]:
        """Sizes of the long-lived collections the worker's services hold.

        Each source exposes ``memory_footprint()``; one that fails is skipped so
        the report still carries the rest.
        """

        sizes: dict[WorkerMemoryComponent, int] = {}
        for source in self._memory_sources:
            footprint: Callable[[], Mapping[WorkerMemoryComponent, int]] | None = (
                getattr(source, "memory_footprint", None)
            )
            if not callable(footprint):
                continue
            try:
                sizes.update(footprint())
            except Exception:
                _LOGGER.warning("memory footprint source failed", exc_info=True)
        return sizes

    def run_once(self) -> bool:
        # Housekeeping runs before all work, so one failing maintenance task
        # must not deny the heartbeat and every source their turn.  Each task
        # is contained and reported; the outer loop retries it next pass.
        self._run_housekeeping("telemetry", self._housekeeping)
        self._run_housekeeping("artifact", self._artifact_housekeeping)
        sources: list[tuple[str, Callable[[], bool]]] = []
        if self._recipes is not None:
            sources.append(("recipes", self._recipes.tick))
        if self._model_cache is not None:
            sources.append(("model-cache", self._run_model_cache))
        for index, service in enumerate(self._background_services):
            sources.append(
                (
                    f"background-{index}",
                    lambda service=service: bool(service()),
                )
            )
        sources.append(("jobs", self._run_generic))
        if self._source_cursor >= len(sources):
            self._source_cursor = 0
        advanced = False
        for offset in range(len(sources)):
            index = (self._source_cursor + offset) % len(sources)
            name, source = sources[index]
            try:
                progressed = source()
            except (UnknownOutcomeError, *_SOURCE_FAILURES) as error:
                # A source that keeps failing must not starve the others: the
                # failure stays visible, the turn moves on, and the failed
                # source is retried on a later pass.
                log_event(
                    _LOGGER,
                    "worker.source_failed",
                    service="control-worker",
                    source=name,
                    error=type(error).__name__,
                    message=redact_text(error),
                    traceback=redact_text(traceback.format_exc()),
                )
                continue
            if progressed:
                self._source_cursor = (index + 1) % len(sources)
                advanced = True
                break
        if self._loop_heartbeat is not None:
            self._loop_heartbeat()
        return advanced

    @staticmethod
    def _run_housekeeping(name: str, task: Callable[[], object] | None) -> None:
        if task is None:
            return
        try:
            task()
        except (UnknownOutcomeError, *_SOURCE_FAILURES) as error:
            log_event(
                _LOGGER,
                "worker.housekeeping_failed",
                service="control-worker",
                task=name,
                error=type(error).__name__,
                message=redact_text(error),
                traceback=redact_text(traceback.format_exc()),
            )

    def _run_model_cache(self) -> bool:
        # The model cache is consumed structurally, like the distribution
        # executor does: either entry point is optional, so probe both rather
        # than assume one shape.
        tick = getattr(self._model_cache, "tick", None)
        if callable(tick):
            return bool(tick())
        run_pending = getattr(self._model_cache, "run_pending", None)
        if callable(run_pending):
            return bool(run_pending(limit=1))
        raise RuntimeError("model cache service exposes no tick or run_pending")

    def _run_generic(self) -> bool:
        attempt = self._jobs.claim(self._worker_id, 30, kinds=tuple(self._handlers))
        if attempt is None:
            return False
        handler = self._handlers[attempt.kind]
        try:
            result = handler(
                HandlerRequest(
                    attempt.job_id,
                    attempt.kind,
                    attempt.payload,
                    attempt.authority_revision,
                    attempt.targets,
                )
            )
        except _SOURCE_FAILURES as error:
            self._jobs.fail(attempt, f"{type(error).__name__}: {error}")
        else:
            self._jobs.succeed(attempt, result)
        return True


def assemble_production_worker(
    *,
    jobs,
    sessions,
    agent_jobs,
    publisher,
    management_policy,
    clock,
    worker_id: str,
    artifact_job_root: Path,
    artifact_job_storage_max_bytes: int,
    artifact_job_retention_seconds: int,
    artifact_job_reconcile_interval_seconds: int,
    artifact_job_reconcile_batch_limit: int,
    distributed_start_timeout_seconds: int = 3600,
    model_cache=None,
    model_cache_root: Path | None = None,
    background_services: Sequence[Callable[[], object]] = (),
    background_closers: Sequence[Callable[[], object]] = (),
    agent_artifact_root: Path | None = None,
    recipe_image_artifact_root: Path | None = None,
    recipe_image_parallel_preparations: int = 4,
    compiled_plan_provider: Callable[..., Mapping[str, WireCompiledExecutionPlan]]
    | None = None,
    runtime_image_preparer: Callable[..., object] | None = None,
    loop_heartbeat: Callable[[], object] | None = None,
    capabilities: CapabilityRegistry | None = None,
) -> Worker:
    """Compose the worker-owned recipe and maintenance runtime."""

    from .artifact_blob_store import ArtifactBlobStore
    from .artifact_jobs import ArtifactJobService
    from .artifact_maintenance import ArtifactMaintenanceCadence
    from .attempt_residues import AttemptResidueReconciler
    from .catalog_revision_collection import CatalogRevisionCollector
    from .cluster_mappings import ClusterMappingService
    from .distributed_recovery import DistributedRecoveryCoordinator
    from .distribution import (
        DistributionService,
        build_distribution_service_from_components,
    )
    from .distribution_executor import CompositeDistributionPhaseExecutor
    from .fleet_profiles import build_production_fleet_profile_service
    from .install_admission import (
        InstallAdmissionService,
    )
    from .recipe_builds import RecipeBuildService
    from .recipe_operation_worker import RecipeOperationWorker
    from .recipe_operations import RecipeOperationService
    from .recipe_routes import AtomicRecipeRoutePublisher, RecipeRouteService
    from .run_admission import RunAdmissionService
    from .run_switch_operations import RunSwitchOperationService
    from .runtime_image_preparation import FilesystemRuntimeImageStorage
    from .source_bundles import DatabaseSourceBundleStore
    from .storage_demands import StorageDemands
    from .telemetry_maintenance import (
        TelemetryMaintenance,
        TelemetryMaintenanceCadence,
    )
    from .terminal_history_collection import TerminalHistoryCollector
    from .unused_storage_collection import UnusedStorageCollector

    registry = capabilities or CapabilityRegistry(clock=clock)
    if model_cache is not None:
        if agent_artifact_root is None:
            raise ValueError("agent artifact root is required with model cache")
        distribution = registry.guard(
            ControllerCapability.DISTRIBUTION,
            DistributionService,
            lambda: build_distribution_service_from_components(
                model_cache,
                sessions,
                agent_artifact_root,
                clock=clock,
            ),
        )
        artifact_phase_executor = CompositeDistributionPhaseExecutor(
            sessions,
            agent_jobs,
            distribution,
            model_cache=model_cache,
            runtime_image_preparer=runtime_image_preparer,
            async_runtime_image_preparation=True,
            clock=clock,
        )
    else:
        artifact_phase_executor = None

    recipe_routes = RecipeRouteService(
        sessions,
        publisher=AtomicRecipeRoutePublisher(publisher),
        management_policy=management_policy,
        clock=clock,
    )
    # Reuse and archive-presence checks read the same image cache the
    # availability service writes, so prefer its explicit root.
    image_cache_root = recipe_image_artifact_root or agent_artifact_root
    runtime_archive_storage = (
        registry.guard(
            ControllerCapability.RUNTIME_IMAGE_STORAGE,
            FilesystemRuntimeImageStorage,
            lambda: FilesystemRuntimeImageStorage(image_cache_root),
        )
        if image_cache_root is not None
        else None
    )
    runtime_archive_available = (
        runtime_archive_storage.build_archive_available
        if runtime_archive_storage is not None
        else None
    )
    recipe_builds = RecipeBuildService(
        sessions,
        bundles=DatabaseSourceBundleStore(sessions),
        inventory_max_age=300,
        build_archive_available=runtime_archive_available,
        prepared_builds=(
            runtime_archive_storage.find_build
            if runtime_archive_storage is not None
            else None
        ),
    )
    lifecycle = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(
            sessions,
            inventory_max_age=300,
            disk_floor_bytes=10_000_000_000,
            compiled_plan_provider=compiled_plan_provider,
        ),
        run_admission=RunAdmissionService(
            sessions,
            inventory_max_age=300,
            memory_floor_bytes=PLATFORM_MEMORY_FLOOR_BYTES,
        ),
        agent_jobs=agent_jobs,
        clock=clock,
        route_publications=recipe_routes,
        builds=recipe_builds,
        mappings=ClusterMappingService(sessions),
        distributed_start_timeout_seconds=distributed_start_timeout_seconds,
    )
    run_switch_operations = RunSwitchOperationService(
        sessions,
        memory_floor_bytes=PLATFORM_MEMORY_FLOOR_BYTES,
        lifecycle=lifecycle,
        clock=clock,
        mappings=ClusterMappingService(sessions),
        model_cache=model_cache,
        build_archive_available=runtime_archive_available,
        artifact_phase_executor=artifact_phase_executor,
    )
    fleet_profiles = build_production_fleet_profile_service(
        sessions,
        clock=clock,
        run_switch_operations=run_switch_operations,
        cache_resolver=(
            model_cache.resolve_latest_cached if model_cache is not None else None
        ),
    )
    recipe_operations = RecipeOperationWorker(
        sessions,
        recipe_routes,
        clock=clock,
        order_reconcile=agent_jobs.reconcile_orders,
        build_cleanup=lifecycle.reconcile_cancelled_builds,
        retirement_cleanup=lifecycle.reconcile_retired_operations,
        stop_admission_cleanup=lifecycle.reconcile_pending_service_stops,
        residue_cleanup=AttemptResidueReconciler(
            sessions,
            abandon_never_installed=lifecycle.abandon_never_installed,
            removal=lifecycle,
            clock=clock,
        ).tick,
        fleet_profiles=fleet_profiles,
        run_switches=run_switch_operations,
        recoveries=DistributedRecoveryCoordinator(
            sessions,
            routes=recipe_routes,
            agent_jobs=agent_jobs,
            clock=clock,
            recovery_run_stops=lifecycle,
            singleton_start_timeout_seconds=distributed_start_timeout_seconds,
        ),
    )
    worker_background_services = tuple(background_services)
    worker_background_closers = tuple(background_closers)
    memory_sources: list[object] = [model_cache, artifact_phase_executor]
    close_artifact_executor = getattr(artifact_phase_executor, "close", None)
    if callable(close_artifact_executor):
        worker_background_closers += (close_artifact_executor,)
    image_store_collector = None
    if image_cache_root is not None:
        from .image_store_collection import ImageStoreCollector
        from .prebuilt_images import PrebuiltImageImporter

        # Builds planned from a catalog prebuilt image are executed here, by
        # pulling the pinned digest into the same image cache.
        prebuilt_importer = PrebuiltImageImporter(
            sessions, image_cache_root, clock=clock
        )
        image_store_collector = registry.guard(
            ControllerCapability.IMAGE_COLLECTION,
            ImageStoreCollector,
            lambda: ImageStoreCollector(sessions, image_cache_root, clock=clock),
        )
        worker_background_services += (
            prebuilt_importer.tick,
            # Reclaims image bytes nothing names any more, hourly.
            image_store_collector.tick,
        )
        worker_background_closers += (prebuilt_importer.close,)
    if recipe_image_artifact_root is not None:
        from .availability_production import build_recipe_image_availability

        image_production = build_recipe_image_availability(
            sessions,
            artifact_root=recipe_image_artifact_root,
            managed_catalog_sync=None,
            recipe_builds=recipe_builds,
            recipe_operations=lifecycle,
            model_cache=model_cache,
            clock=clock,
            max_parallel=recipe_image_parallel_preparations,
            with_scheduler=True,
            storage=runtime_archive_storage,
        )
        assert image_production.scheduler is not None
        fleet_profiles.bind_preparation_starter(
            image_production.service.ensure_preparation
        )
        fleet_profiles.bind_preparation_canceller(
            image_production.service.cancel_profile_preparation
        )
        memory_sources += [image_production.service, image_production.scheduler]
        worker_background_services += (image_production.scheduler.tick,)
        worker_background_closers += (image_production.close,)
    # Frees disk when it is short (a Spark or the NAS runs low, or work was
    # refused for lack of it) by removing the least recently used installations,
    # image receipts and cached models nothing uses. Superseded catalog
    # revisions nothing uses any more are database rows, tidied hourly.
    storage_demands = StorageDemands(clock)
    lifecycle.bind_storage_demands(storage_demands)
    recipe_builds.bind_storage_demands(storage_demands)
    if model_cache is not None:
        model_cache.bind_storage_demands(storage_demands)
    storage_collector = UnusedStorageCollector(
        sessions,
        clock=clock,
        lifecycle=lifecycle,
        image_cache_root=image_cache_root,
        model_cache_root=model_cache_root,
        model_cache=model_cache,
        image_blobs=image_store_collector,
        demands=storage_demands,
    )
    memory_sources += [storage_demands, storage_collector]
    fleet_profiles.bind_storage_relief(storage_collector.relief_for_spark)
    worker_background_services += (
        storage_collector.tick,
        CatalogRevisionCollector(sessions, clock=clock).tick,
        TerminalHistoryCollector(sessions, clock=clock).tick,
    )
    telemetry_maintenance = TelemetryMaintenance(sessions, clock=clock)
    artifact_jobs = ArtifactJobService(
        sessions,
        recipe_operations=lifecycle,
        blob_store=registry.guard(
            ControllerCapability.ARTIFACT_STORAGE,
            ArtifactBlobStore,
            lambda: ArtifactBlobStore(
                artifact_job_root, max_stored_bytes=artifact_job_storage_max_bytes
            ),
        ),
        clock=clock,
        retention_seconds=artifact_job_retention_seconds,
    )
    return Worker(
        jobs,
        worker_id,
        {},
        housekeeping=TelemetryMaintenanceCadence(
            telemetry_maintenance,
            clock=clock,
        ),
        artifact_housekeeping=ArtifactMaintenanceCadence(
            artifact_jobs.reconcile_storage,
            state_root=artifact_job_root,
            interval_seconds=artifact_job_reconcile_interval_seconds,
            batch_limit=artifact_job_reconcile_batch_limit,
            clock=clock,
        ),
        recipes=recipe_operations,
        model_cache=model_cache,
        background_services=worker_background_services,
        background_closers=worker_background_closers,
        loop_heartbeat=loop_heartbeat,
        memory_sources=memory_sources,
    )


if __name__ == "__main__":
    import os
    from datetime import UTC, datetime
    from pathlib import Path

    from .agent_jobs import AgentJobService
    from .db import build_engine, session_factory, wait_for_database
    from .execution_plan_service import ControllerExecutionPlanService
    from .logging import configure_controller_logging
    from .model_cache import ModelCacheService
    from .presence import ManagementAddressPolicy
    from .route_runtime import (
        AtomicRouteBundlePublisher,
        FileSupervisorAcknowledger,
    )
    from .runtime_image_preparation import (
        FilesystemRuntimeImageStorage,
        OciLayoutImageTransport,
        make_runtime_image_receipt_preparer,
        stored_runtime_image_resolver,
    )
    from .settings import (
        ARTIFACT_JOB_RECONCILE_BATCH_LIMIT,
        ARTIFACT_JOB_RECONCILE_INTERVAL_SECONDS,
        ARTIFACT_JOB_RETENTION_SECONDS,
        ARTIFACT_JOB_STORAGE_MAX_BYTES,
        DISTRIBUTED_START_TIMEOUT_SECONDS,
        MODEL_CACHE_MAX_DOWNLOAD_STREAMS,
        MODEL_CACHE_PARALLEL_DOWNLOADS,
        MODEL_CACHE_RESERVE_BYTES,
        RECIPE_IMAGE_PARALLEL_PREPARATIONS,
        WORKER_MEMORY_SAMPLE_INTERVAL_SECONDS,
        WORKER_MEMORY_TRACE_FRAMES,
        WORKER_MEMORY_TRACE_REARM_BYTES,
        WORKER_MEMORY_TRACE_RSS_BYTES,
        WORKER_MEMORY_TRACE_WINDOW_SECONDS,
        Settings,
    )
    from .worker_memory import (
        LinuxProcessProbe,
        WorkerMemoryMonitor,
        start_worker_memory_sampler,
        worker_memory_report_path,
        worker_memory_trace_request_path,
    )

    configure_controller_logging()
    capabilities = CapabilityRegistry()
    settings = Settings.from_env_and_secrets()
    wait_for_database(settings.database_url)
    sessions = session_factory(build_engine(settings.database_url, component="worker"))

    def clock() -> datetime:
        return datetime.now(UTC)

    jobs = JobService(sessions, clock=clock)
    address_policy = capabilities.guard(
        ControllerCapability.MANAGEMENT_POLICY,
        ManagementAddressPolicy,
        lambda: ManagementAddressPolicy.parse(
            settings.management_cidrs,
            forbidden_cidrs=settings.direct_fabric_cidrs,
        ),
    )

    agent_jobs = AgentJobService(
        sessions,
        clock=clock,
    )
    route_root = Path("/routes")
    publisher = capabilities.guard(
        ControllerCapability.ROUTE_PUBLISHER,
        AtomicRouteBundlePublisher,
        lambda: AtomicRouteBundlePublisher(
            route_root,
            await_supervisor_ack=FileSupervisorAcknowledger(
                Path("/supervisor/ack.json"),
                clock=clock,
            ),
        ),
    )
    runtime_image_storage = capabilities.guard(
        ControllerCapability.RUNTIME_IMAGE_STORAGE,
        FilesystemRuntimeImageStorage,
        lambda: FilesystemRuntimeImageStorage(settings.agent_artifact_root),
    )

    def build_cache() -> ModelCacheService:
        cache = ModelCacheService(
            sessions,
            settings.model_cache_root,
            reserve_bytes=MODEL_CACHE_RESERVE_BYTES,
            max_parallel_downloads=MODEL_CACHE_PARALLEL_DOWNLOADS,
            max_download_streams=MODEL_CACHE_MAX_DOWNLOAD_STREAMS,
            clock=clock,
            huggingface_token_path=settings.huggingface_token_path,
            runtime_archive_available=runtime_image_storage.build_archive_available,
        )
        return cache

    model_cache = capabilities.guard(
        ControllerCapability.MODEL_CACHE,
        ModelCacheService,
        build_cache,
        initialize=lambda cache: cache.resume_operations(),
    )
    runtime_image_transport = OciLayoutImageTransport()
    prepare_runtime_image_receipt = make_runtime_image_receipt_preparer(
        runtime_image_storage,
        runtime_image_transport,
        clock=clock,
    )

    execution_plans = ControllerExecutionPlanService(
        model_cache,
        runtime_image_resolver=stored_runtime_image_resolver(runtime_image_storage),
    )
    worker = assemble_production_worker(
        capabilities=capabilities,
        distributed_start_timeout_seconds=DISTRIBUTED_START_TIMEOUT_SECONDS,
        jobs=jobs,
        sessions=sessions,
        agent_jobs=agent_jobs,
        publisher=publisher,
        management_policy=address_policy,
        clock=clock,
        worker_id=os.environ.get("HOSTNAME", "control-worker"),
        artifact_job_root=settings.state_path / "artifact-jobs" / "blobs",
        artifact_job_storage_max_bytes=ARTIFACT_JOB_STORAGE_MAX_BYTES,
        artifact_job_retention_seconds=ARTIFACT_JOB_RETENTION_SECONDS,
        artifact_job_reconcile_interval_seconds=ARTIFACT_JOB_RECONCILE_INTERVAL_SECONDS,
        artifact_job_reconcile_batch_limit=ARTIFACT_JOB_RECONCILE_BATCH_LIMIT,
        model_cache=model_cache,
        model_cache_root=settings.model_cache_root,
        agent_artifact_root=settings.agent_artifact_root,
        recipe_image_artifact_root=settings.agent_artifact_root,
        recipe_image_parallel_preparations=RECIPE_IMAGE_PARALLEL_PREPARATIONS,
        compiled_plan_provider=execution_plans.compile_installation,
        runtime_image_preparer=prepare_runtime_image_receipt,
        loop_heartbeat=WorkerHeartbeatRecorder(
            sessions,
            process_instance_id=current_worker_instance_id(),
            clock=clock,
        ).completed_loop,
    )
    watchdog = WorkerWatchdog(timeout_seconds=_WORKER_WATCHDOG_TIMEOUT_SECONDS)
    watchdog_stop = threading.Event()
    memory_monitor = WorkerMemoryMonitor(
        report_path=worker_memory_report_path(settings.state_path),
        trace_request_path=worker_memory_trace_request_path(settings.state_path),
        probe=LinuxProcessProbe(),
        components=worker.memory_footprint,
        clock=clock,
        trace_rss_bytes=WORKER_MEMORY_TRACE_RSS_BYTES,
        trace_rearm_bytes=WORKER_MEMORY_TRACE_REARM_BYTES,
        trace_window_seconds=WORKER_MEMORY_TRACE_WINDOW_SECONDS,
        trace_frames=WORKER_MEMORY_TRACE_FRAMES,
    )
    memory_thread = start_worker_memory_sampler(
        memory_monitor,
        interval_seconds=WORKER_MEMORY_SAMPLE_INTERVAL_SECONDS,
        stop=watchdog_stop,
    )

    def monitor_worker_loop() -> None:
        while not watchdog_stop.wait(5):
            if watchdog.stalled():
                log_event(
                    _LOGGER,
                    "worker.watchdog_stalled",
                    service="control-worker",
                    timeout_seconds=_WORKER_WATCHDOG_TIMEOUT_SECONDS,
                    action="exiting non-zero for container restart",
                )
                os._exit(70)

    watchdog_thread = threading.Thread(
        target=monitor_worker_loop, name="worker-watchdog", daemon=True
    )
    watchdog_thread.start()
    try:
        while True:
            try:
                if not worker.run_once():
                    time.sleep(1)
                watchdog.beat()
            except Exception as error:  # noqa: BLE001 - a tick must not kill the worker
                # Retried on the next pass. A worker that exits here stops
                # draining every coordinator, so the failure is reported and the
                # loop continues rather than taking the whole scheduler down.
                log_event(
                    _LOGGER,
                    "worker.tick_failed",
                    service="control-worker",
                    error=type(error).__name__,
                    message=redact_text(error),
                    traceback=redact_text(traceback.format_exc()),
                )
                time.sleep(1)
                watchdog.beat()
    finally:
        watchdog_stop.set()
        watchdog_thread.join(timeout=2)
        memory_thread.join(timeout=2)
        worker.close()
