"""Api: production concerns."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from vonk_agent_protocol import UnknownOutcomeError

from ..agent_services import build_agent_services
from ..artifact_blob_store import ArtifactBlobStore
from ..artifact_jobs import ArtifactJobService
from ..auth import CursorCodec, TokenCodec
from ..bounded_retry import REQUEST_PAUSES
from ..browser_auth import BrowserAuthService
from ..capabilities import CapabilityRegistry
from ..capability_contract import ControllerCapability
from ..catalog_service import CatalogService
from ..catalog_sync import ManagedRecipeCatalogSyncService, run_automatic_sync
from ..cluster_mappings import ClusterMappingService
from ..distribution_executor import CompositeDistributionPhaseExecutor
from ..failure_evidence import FailureEvidenceService
from ..gateway_keys import GatewayKeyService, keep_default_key
from ..library_assessment import LibraryAssessment
from ..logging import configure_controller_logging
from ..metrics import MetricsRegistry, runnable_job_ages
from ..model_cache import ModelCacheService
from ..model_cache_api import register_model_cache_operation_provider
from ..operator_projection_api import build_fleet_operator_services
from ..platform_observation import PlatformObserver
from ..recipe_builds import RecipeBuildService
from ..recipe_operations import RecipeOperationService
from ..recipe_packages import RecipePackageClient
from ..resource_planning import PLATFORM_MEMORY_FLOOR_BYTES
from ..run_switch_operations import RunSwitchOperationService
from ..settings import (
    AGENT_RELEASE_API_URL,
    ARTIFACT_JOB_RETENTION_SECONDS,
    ARTIFACT_JOB_STORAGE_MAX_BYTES,
    DISTRIBUTED_START_TIMEOUT_SECONDS,
    MODEL_CACHE_MAX_DOWNLOAD_STREAMS,
    MODEL_CACHE_PARALLEL_DOWNLOADS,
    MODEL_CACHE_RESERVE_BYTES,
    RECIPE_IMAGE_PARALLEL_PREPARATIONS,
    RECIPE_LIBRARY_API_URL,
    RECIPE_LIBRARY_ASSET_URL,
    RECIPE_LIBRARY_SYNC_INTERVAL_SECONDS,
    WORKER_MEMORY_REPORT_MAX_AGE_SECONDS,
    Settings,
)
from ..source_bundles import DatabaseSourceBundleStore
from .application import create_app
from .common import _LOGGER, SpaFiles, refresh_fleet_metrics


async def _close_model_cache(model_cache: ModelCacheService) -> None:
    """Bound shutdown checkpoint retries; durable transfers resume on restart."""
    from ..logging import log_event, redact_text

    for attempt in range(len(REQUEST_PAUSES) + 1):
        try:
            model_cache.close()
            return
        except UnknownOutcomeError as error:
            log_event(
                _LOGGER,
                "model_cache.shutdown_checkpoint_deferred",
                service="control-api",
                attempt=attempt + 1,
                detail=redact_text(error),
                resume="Controller restart"
                if attempt == len(REQUEST_PAUSES)
                else "next shutdown attempt",
            )
        if attempt < len(REQUEST_PAUSES):
            await asyncio.sleep(REQUEST_PAUSES[attempt])


def production_app(settings: Settings | None = None) -> FastAPI:
    configure_controller_logging()
    capabilities = CapabilityRegistry()
    from sqlalchemy import func, select

    from ..agent_upgrades import AgentUpgradeService
    from ..availability_production import build_recipe_image_availability
    from ..db import build_engine, session_factory
    from ..execution_plan_service import ControllerExecutionPlanService
    from ..fleet_events import FleetEventRepository
    from ..fleet_projection import FleetProjection
    from ..fleet_stream import FleetStream
    from ..install_admission import (
        InstallAdmissionService,
    )
    from ..jobs import JobService
    from ..library_projection import LibraryProjection
    from ..metrics import OperationalMetricsCollector
    from ..models import Job
    from ..operation_api import durable_operation_services
    from ..presence import ManagementAddressPolicy
    from ..recipe_routes import AtomicRecipeRoutePublisher, RecipeRouteService
    from ..route_runtime import AtomicRouteBundlePublisher, FileSupervisorAcknowledger
    from ..run_admission import RunAdmissionService
    from ..runtime_image_preparation.preparation import (
        make_runtime_image_receipt_preparer,
        stored_runtime_image_resolver,
    )
    from ..runtime_image_preparation.storage import FilesystemRuntimeImageStorage
    from ..runtime_image_preparation.transport import OciLayoutImageTransport
    from ..telemetry import TelemetryRepository
    from ..worker_memory import read_worker_memory_report, worker_memory_report_path

    if settings is None:
        settings = Settings.from_env_and_secrets()
    sessions = session_factory(build_engine(settings.database_url, component="api"))
    # Planning, profile choices, and preparation share the same managed OCI root.
    runtime_image_storage = capabilities.guard(
        ControllerCapability.RUNTIME_IMAGE_STORAGE,
        FilesystemRuntimeImageStorage,
        lambda: FilesystemRuntimeImageStorage(settings.agent_artifact_root),
    )

    def clock() -> datetime:
        return datetime.now(UTC)

    token_codec = capabilities.guard(
        ControllerCapability.TOKEN_AUTH,
        TokenCodec,
        lambda: TokenCodec(settings.token_signing_key),
    )
    cursor_codec = capabilities.guard(
        ControllerCapability.CURSOR_AUTH,
        CursorCodec,
        token_codec.cursor_codec,
    )
    job_service = JobService(sessions, clock=clock)
    database_bundles = DatabaseSourceBundleStore(sessions)
    telemetry_repository = TelemetryRepository(sessions, clock=clock)
    fleet_event_repository = FleetEventRepository(sessions, clock=clock)
    visual_fleet = FleetProjection(
        sessions,
        clock=clock,
        events=fleet_event_repository,
        telemetry=telemetry_repository,
    )
    visual_fleet_stream = FleetStream(
        fleet_event_repository,
        telemetry_repository,
        clock=clock,
    )
    metrics = MetricsRegistry()
    operational_metrics = OperationalMetricsCollector(
        metrics,
        sessions,
        clock=clock,
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

    agent_services = build_agent_services(
        settings,
        sessions,
        clock,
        model_cache=model_cache,
        capabilities=capabilities,
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

    recipe_route_runtime = capabilities.guard(
        ControllerCapability.ROUTE_PUBLISHER,
        AtomicRouteBundlePublisher,
        lambda: AtomicRouteBundlePublisher(
            Path("/routes"),
            await_supervisor_ack=FileSupervisorAcknowledger(
                Path("/supervisor/ack.json"), clock=clock
            ),
        ),
    )
    recipe_routes = capabilities.guard(
        ControllerCapability.RECIPE_ROUTES,
        RecipeRouteService,
        lambda: RecipeRouteService(
            sessions,
            publisher=AtomicRecipeRoutePublisher(recipe_route_runtime),
            management_policy=ManagementAddressPolicy.parse(
                settings.management_cidrs,
                forbidden_cidrs=settings.direct_fabric_cidrs,
            ),
            clock=clock,
        ),
    )
    recipe_builds = RecipeBuildService(
        sessions,
        bundles=database_bundles,
        inventory_max_age=300,
        build_archive_available=runtime_image_storage.build_archive_available,
        prepared_builds=runtime_image_storage.find_build,
    )
    recipe_operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(
            sessions,
            inventory_max_age=300,
            disk_floor_bytes=10_000_000_000,
            compiled_plan_provider=execution_plans.compile_installation,
        ),
        run_admission=RunAdmissionService(
            sessions,
            inventory_max_age=300,
            memory_floor_bytes=PLATFORM_MEMORY_FLOOR_BYTES,
        ),
        agent_jobs=agent_services.operations,
        clock=clock,
        route_publications=recipe_routes,
        builds=recipe_builds,
        mappings=ClusterMappingService(sessions),
        distributed_start_timeout_seconds=DISTRIBUTED_START_TIMEOUT_SECONDS,
    )
    run_switch_operations = RunSwitchOperationService(
        sessions,
        memory_floor_bytes=PLATFORM_MEMORY_FLOOR_BYTES,
        lifecycle=recipe_operations,
        clock=clock,
        mappings=ClusterMappingService(sessions),
        model_cache=model_cache,
        build_archive_available=runtime_image_storage.build_archive_available,
        artifact_phase_executor=CompositeDistributionPhaseExecutor(
            sessions,
            agent_services.operations,
            agent_services.distribution,
            model_cache=model_cache,
            runtime_image_preparer=prepare_runtime_image_receipt,
            clock=clock,
        ),
    )
    visual_library = LibraryProjection(
        sessions,
        cursors=cursor_codec,
        clock=clock,
        runtime_archive_available=runtime_image_storage.build_archive_available,
        # The Library reports stored images; it never gates on them. Recent
        # answers are reused so a read does not re-walk network storage.
        image_present_ttl_seconds=30.0,
        image_absent_ttl_seconds=5.0,
        assessment=LibraryAssessment(
            sessions,
            run_switch=run_switch_operations,
            model_cache=model_cache,
            clock=clock,
        ),
    )

    def build_artifact_jobs() -> ArtifactJobService:
        service = ArtifactJobService(
            sessions,
            recipe_operations=recipe_operations,
            blob_store=ArtifactBlobStore(
                settings.state_path / "artifact-jobs" / "blobs",
                max_stored_bytes=ARTIFACT_JOB_STORAGE_MAX_BYTES,
            ),
            clock=clock,
            retention_seconds=ARTIFACT_JOB_RETENTION_SECONDS,
        )
        return service

    artifact_jobs = capabilities.guard(
        ControllerCapability.ARTIFACT_STORAGE,
        ArtifactJobService,
        build_artifact_jobs,
        initialize=lambda service: service.reconcile_storage(),
    )
    from ..fleet_profiles import build_production_fleet_profile_service

    fleet_profiles = build_production_fleet_profile_service(
        sessions,
        clock=clock,
        run_switch_operations=run_switch_operations,
        cache_resolver=model_cache.resolve_latest_cached,
    )
    agent_upgrades = AgentUpgradeService(
        sessions,
        agent_services.operations,
        clock=clock,
        channel=settings.install_channel,
        release_api_url=AGENT_RELEASE_API_URL,
    )

    def consume_agent_result(session, operation, attempt, message) -> None:
        artifact_jobs.consume_agent_result(session, operation, attempt, message)
        recipe_operations.consume_agent_result(session, operation, attempt, message)
        agent_upgrades.consume_agent_result(session, operation, attempt, message)

    agent_services.operations.set_result_consumer(consume_agent_result)

    def refresh_metrics() -> None:
        operational_metrics.refresh()
        now = datetime.now(UTC)
        metrics.set_worker_memory(
            read_worker_memory_report(
                worker_memory_report_path(settings.state_path),
                now=now,
                max_age_seconds=WORKER_MEMORY_REPORT_MAX_AGE_SECONDS,
            ),
            now,
        )
        refresh_fleet_metrics(metrics, visual_fleet.read())
        now = datetime.now(UTC)
        with sessions() as session:
            job_counts = [
                (kind, state, count)
                for kind, state, count in session.execute(
                    select(Job.kind, Job.state, func.count()).group_by(
                        Job.kind, Job.state
                    )
                )
            ]
            metrics.replace_job_counts(job_counts)
            # Oldest queued work an operator could actually run now.  A job
            # deferred by a future ``observation_due_at`` is an intentional wait,
            # not starvation, and must not age into the alert.
            metrics.replace_runnable_job_ages(
                runnable_job_ages(
                    (
                        (row.kind, row.created_at, row.result)
                        for row in session.execute(
                            select(Job.kind, Job.created_at, Job.result).where(
                                Job.state == "queued"
                            )
                        )
                    ),
                    now,
                ).items()
            )
        backup_marker = settings.state_path / "last-successful-backup.epoch"
        backup_completed_at: int | None = None
        if backup_marker.is_file() and not backup_marker.is_symlink():
            try:
                backup_completed_at = int(backup_marker.read_text().strip())
                if backup_completed_at < 0:
                    backup_completed_at = None
            except (OSError, ValueError):
                pass
        metrics.set_backup_successful(backup_completed_at is not None)
        metrics.set_backup_age(
            None
            if backup_completed_at is None
            else max(0, int(time.time()) - backup_completed_at)
        )
        restore_marker = settings.state_path / "last-backup-restore-verification.epoch"
        restore_completed_at: int | None = None
        if restore_marker.is_file() and not restore_marker.is_symlink():
            try:
                restore_completed_at = int(restore_marker.read_text().strip())
                if restore_completed_at < 0:
                    restore_completed_at = None
            except (OSError, ValueError):
                pass
        metrics.set_backup_restore_verified(restore_completed_at is not None)
        metrics.set_backup_restore_verification_age(
            None
            if restore_completed_at is None
            else max(0, int(time.time()) - restore_completed_at)
        )

    recipe_library = capabilities.guard(
        ControllerCapability.RECIPE_LIBRARY,
        RecipePackageClient,
        lambda: RecipePackageClient(
            cache_root=settings.state_path / "recipe-library-packages",
            api_url=RECIPE_LIBRARY_API_URL,
            asset_url=RECIPE_LIBRARY_ASSET_URL,
            release=settings.recipe_library_release,
        ),
    )
    catalog_service = CatalogService(
        sessions,
        clock=clock,
        cursors=cursor_codec,
        source_bundles=database_bundles,
    )
    managed_catalog_sync = ManagedRecipeCatalogSyncService(
        sessions,
        catalog=catalog_service,
        reader=recipe_library,
        clock=clock,
    )
    recipe_image_production = build_recipe_image_availability(
        sessions,
        settings=settings,
        managed_catalog_sync=managed_catalog_sync,
        recipe_builds=recipe_builds,
        recipe_operations=recipe_operations,
        model_cache=model_cache,
        clock=clock,
        max_parallel=RECIPE_IMAGE_PARALLEL_PREPARATIONS,
        storage=runtime_image_storage,
    )
    # A load asks for the preparation it needs instead of stopping at its absence.
    fleet_profiles.bind_preparation_starter(
        recipe_image_production.service.ensure_preparation
    )
    fleet_profiles.bind_preparation_canceller(
        recipe_image_production.service.cancel_profile_preparation
    )

    automatic_sync_task: asyncio.Task[None] | None = None
    automatic_sync_stop = asyncio.Event()

    gateway_keys = capabilities.guard(
        ControllerCapability.GATEWAY_KEYS,
        GatewayKeyService,
        GatewayKeyService,
        check=lambda service: service.check_health(),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal automatic_sync_task
        capabilities.start_recovery()
        automatic_sync_task = asyncio.create_task(
            run_automatic_sync(
                managed_catalog_sync,
                automatic_sync_stop,
                interval_seconds=RECIPE_LIBRARY_SYNC_INTERVAL_SECONDS,
            )
        )
        default_key_task = asyncio.create_task(
            keep_default_key(gateway_keys, automatic_sync_stop)
        )
        try:
            yield
        finally:
            automatic_sync_stop.set()
            await capabilities.stop_recovery()
            try:
                await default_key_task
            except HTTPException:
                pass
            if automatic_sync_task is not None:
                await automatic_sync_task
            await _close_model_cache(model_cache)
            recipe_image_production.close()
            recipe_library.close()
            agent_upgrades.close()

    browser_auth = capabilities.guard(
        ControllerCapability.BROWSER_AUTH,
        BrowserAuthService,
        lambda: BrowserAuthService(
            sessions, token_signing_key=settings.token_signing_key, clock=clock
        ),
    )
    metrics_secret = capabilities.provider(
        ControllerCapability.METRICS_AUTH,
        str,
        lambda: settings.metrics_token,
    )
    proxy_secret = capabilities.provider(
        ControllerCapability.AGENT_PROXY_AUTH,
        bytes,
        lambda: settings.agent_proxy_auth,
    )
    app = create_app(
        platform_observer=PlatformObserver(
            sessions, clock=clock, capabilities=capabilities
        ),
        jobs=job_service,
        tokens=token_codec,
        fleet_projection=visual_fleet,
        fleet_stream=visual_fleet_stream,
        library_projection=visual_library,
        metrics=metrics,
        metrics_token=metrics_secret.require_service,
        metrics_refresh=refresh_metrics,
        agent=(agent_services if settings.agent_runtime_enabled else None),
        trusted_agent_proxy_auth=proxy_secret.require_service,
        operations=register_model_cache_operation_provider(
            durable_operation_services(
                sessions,
                Path("/routes"),
                clock=clock,
                cursors=cursor_codec,
                resume_agent_upgrade=agent_upgrades.resume,
                operation_providers=(
                    fleet_profiles.operation_provider(),
                    run_switch_operations.activity_provider(),
                    recipe_image_production.service.update_activity_provider(),
                ),
                profile_endpoint_intent=fleet_profiles.endpoint_intent,
            ),
            model_cache,
        ),
        catalog=catalog_service,
        recipe_library=recipe_library,
        managed_catalog_sync=managed_catalog_sync,
        browser_auth=browser_auth,
        recipe_operations=recipe_operations,
        run_switch_operations=run_switch_operations,
        artifact_jobs=artifact_jobs,
        fleet_profiles=fleet_profiles,
        fleet_services=build_fleet_operator_services(
            agent_services=(agent_services if settings.agent_runtime_enabled else None),
            upgrades=agent_upgrades,
            sessions=sessions,
        ),
        failure_evidence=FailureEvidenceService(sessions),
        agent_upgrades=agent_upgrades,
        model_cache=model_cache,
        recipe_image_availability=recipe_image_production.service,
        gateway_keys=gateway_keys,
        lifespan=lifespan,
    )
    app.state.capabilities = capabilities

    web_root = Path(__file__).resolve().parent / "web"
    if web_root.is_dir():
        app.mount("/", SpaFiles(directory=web_root, html=True), name="admin-web")

    return app
