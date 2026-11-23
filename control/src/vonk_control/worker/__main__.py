from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from vonk_agent_protocol.capability_reply import ControllerCapability

from ..capabilities import CapabilityRegistry
from ..jobs.queue import JobService
from ..logging import log_event, redact_text

if TYPE_CHECKING:
    from ..catalog.sync import ManagedRecipeCatalogSyncService
    from ..enrollment.service import EnrollmentService
from .assembly import assemble_production_worker
from .liveness import (
    _LOGGER,
    _WORKER_WATCHDOG_TIMEOUT_SECONDS,
    WorkerHeartbeatRecorder,
    WorkerWatchdog,
    current_worker_instance_id,
    watch_worker,
)

if __name__ == "__main__":
    import os
    from datetime import UTC, datetime
    from pathlib import Path

    from ..agent_jobs.service import AgentJobService
    from ..db.connections import build_engine, session_factory, wait_for_database
    from ..execution_plan_service import ControllerExecutionPlanService
    from ..gateway.route_runtime import (
        AtomicRouteBundlePublisher,
        FileSupervisorAcknowledger,
    )
    from ..logging import configure_controller_logging
    from ..model_cache.service import ModelCacheService
    from ..presence import ManagementAddressPolicy
    from ..runtime_image_preparation.preparation import (
        make_runtime_image_receipt_preparer,
        stored_runtime_image_resolver,
    )
    from ..runtime_image_preparation.storage import FilesystemRuntimeImageStorage
    from ..runtime_image_preparation.transport import OciLayoutImageTransport
    from ..settings import (
        ARTIFACT_JOB_RECONCILE_BATCH_LIMIT,
        ARTIFACT_JOB_RECONCILE_INTERVAL_SECONDS,
        ARTIFACT_JOB_RETENTION_SECONDS,
        ARTIFACT_JOB_STORAGE_MAX_BYTES,
        DISTRIBUTED_START_TIMEOUT_SECONDS,
        FREE_SPACE_FOR_COLOCATED_SERVICES_FRACTION,
        MODEL_CACHE_MAX_DOWNLOAD_STREAMS,
        MODEL_CACHE_PARALLEL_DOWNLOADS,
        RECIPE_IMAGE_PARALLEL_PREPARATIONS,
        WORKER_MEMORY_SAMPLE_INTERVAL_SECONDS,
        WORKER_MEMORY_TRACE_FRAMES,
        WORKER_MEMORY_TRACE_REARM_BYTES,
        WORKER_MEMORY_TRACE_RSS_BYTES,
        WORKER_MEMORY_TRACE_WINDOW_SECONDS,
        Settings,
    )
    from .memory import (
        LinuxProcessProbe,
        WorkerMemoryMonitor,
        start_worker_memory_sampler,
        worker_memory_report_path,
        worker_memory_trace_request_path,
    )

    configure_controller_logging()
    capabilities = CapabilityRegistry()
    # Compose starts this worker after runtime-init staged its secrets, beside
    # the API that owns the schema. Wait for the schema instead of crashing.
    settings = Settings.from_env_and_secrets()
    wait_for_database(settings.database_url)
    engine = build_engine(settings.database_url, component="worker")
    sessions = session_factory(engine)

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
        return ModelCacheService(
            sessions,
            settings.model_cache_root,
            free_fraction=FREE_SPACE_FOR_COLOCATED_SERVICES_FRACTION,
            max_parallel_downloads=MODEL_CACHE_PARALLEL_DOWNLOADS,
            max_download_streams=MODEL_CACHE_MAX_DOWNLOAD_STREAMS,
            clock=clock,
            huggingface_token_path=settings.huggingface_token_path,
            runtime_archive_available=runtime_image_storage.build_archive_available,
        )

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
    from ..agent_services import build_enrollment_service
    from ..enrollment.service import EnrollmentService

    enrollment = capabilities.guard(
        ControllerCapability.CERTIFICATE_AUTHORITY,
        EnrollmentService,
        lambda: build_enrollment_service(settings, sessions, clock),
    )

    def reconcile_enrollment() -> bool:
        if not settings.agent_runtime_enabled:
            return False
        return enrollment.reconcile_revocations()

    from ..auth import TokenCodec
    from ..catalog.service import CatalogService
    from ..catalog.sync import ManagedRecipeCatalogSyncService
    from ..recipe_packages.client import RecipePackageClient
    from ..source_bundles import DatabaseSourceBundleStore

    catalog_sync = capabilities.guard(
        ControllerCapability.RECIPE_LIBRARY,
        ManagedRecipeCatalogSyncService,
        lambda: ManagedRecipeCatalogSyncService(
            sessions,
            catalog=CatalogService(
                sessions,
                clock=clock,
                cursors=TokenCodec(settings.token_signing_key).cursor_codec(),
                source_bundles=DatabaseSourceBundleStore(sessions),
            ),
            # GitHub directly over the worker's artifact-egress network; the
            # Caddy relay serves only the API, which has no egress.
            reader=RecipePackageClient(
                cache_root=settings.state_path / "recipe-library-packages",
                release=settings.recipe_library_release,
            ),
            clock=clock,
        ),
    )
    watchdog = WorkerWatchdog(timeout_seconds=_WORKER_WATCHDOG_TIMEOUT_SECONDS)
    process_instance_id = current_worker_instance_id()
    heartbeat_recorder = WorkerHeartbeatRecorder(
        sessions,
        process_instance_id=process_instance_id,
        clock=clock,
    )
    worker = assemble_production_worker(
        catalog_sync=catalog_sync,
        enrollment=enrollment,
        background_services=(reconcile_enrollment,),
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
        loop_heartbeat=heartbeat_recorder.completed_loop,
        step_heartbeat=watchdog.beat,
        process_instance_id=process_instance_id,
    )
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

    watchdog_thread = threading.Thread(
        target=watch_worker,
        args=(watchdog, watchdog_stop),
        kwargs={"record_step": heartbeat_recorder.record_running_step},
        name="worker-watchdog",
        daemon=True,
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
