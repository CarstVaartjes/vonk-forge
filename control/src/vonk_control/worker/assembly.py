from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from vonk_agent_protocol import (
    UnknownOutcomeError,
    WaitOwnerKind,
    WaitReason,
)
from vonk_agent_protocol.capability_reply import ControllerCapability
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)
from vonk_forge_contracts.memory_admission import PLATFORM_MEMORY_FLOOR_BYTES

from ..capabilities import CapabilityRegistry
from ..coordinator_failures import CoordinatorFailures
from ..inventory.repository import INVENTORY_MAX_AGE_SECONDS

if TYPE_CHECKING:
    from ..catalog.sync import ManagedRecipeCatalogSyncService
    from ..enrollment.service import EnrollmentService
from .loop import Worker


@cache
def _coordinator_source_content() -> str:
    """One startup identity of executable Controller content, independent of release provenance."""
    root = Path(__file__).parent.parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


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
    step_heartbeat: Callable[[str], object] | None = None,
    process_instance_id: str | None = None,
    capabilities: CapabilityRegistry | None = None,
    catalog_sync: ManagedRecipeCatalogSyncService | None = None,
    enrollment: EnrollmentService | None = None,
) -> Worker:
    """Compose the worker-owned recipe and maintenance runtime."""

    from ..agent_jobs.persistence import _OrderStore
    from ..agent_jobs.retirement import operator_resume_candidates_in_session
    from ..agent_upgrades.service import AgentUpgradeService
    from ..artifact_jobs.blob_store import ArtifactBlobStore
    from ..artifact_jobs.maintenance import ArtifactMaintenanceCadence
    from ..artifact_jobs.service import ArtifactJobService
    from ..attempt_residues import AttemptResidueReconciler
    from ..catalog.revision_collection import CatalogRevisionCollector
    from ..cluster_mappings import ClusterMappingService
    from ..distributed_recovery.coordinator import DistributedRecoveryCoordinator
    from ..distribution.service import DistributionService
    from ..distribution.wiring import build_distribution_service_from_components
    from ..distribution_executor.preparation import CompositeDistributionPhaseExecutor
    from ..due_waits import AdvanceWait, DueWaitBackstop, RowKey
    from ..fleet_profiles.factory import build_production_fleet_profile_service
    from ..install_admission.service import InstallAdmissionService
    from ..lifecycle.agent_operation import AgentOperationAdapter
    from ..lifecycle.artifact_job import ArtifactJobAdapter
    from ..recipe_builds.service import RecipeBuildService
    from ..recipe_operation_worker import RecipeOperationWorker
    from ..recipe_operations.service import RecipeOperationService
    from ..recipe_routes.publisher import AtomicRecipeRoutePublisher
    from ..recipe_routes.service import RecipeRouteService
    from ..run_admission import RunAdmissionService
    from ..run_switch_operations.service import RunSwitchOperationService
    from ..runtime_image_preparation.storage import FilesystemRuntimeImageStorage
    from ..source_bundles import DatabaseSourceBundleStore
    from ..storage_demands import StorageDemands
    from ..telemetry.maintenance import (
        TelemetryMaintenance,
        TelemetryMaintenanceCadence,
    )
    from ..terminal_history_collection import TerminalHistoryCollector
    from ..unused_storage_collection.service import UnusedStorageCollector
    from ..wait_redrive import wait_sources

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

    from ..gateway.route_grants import require_existing_recipe_routes

    recipe_routes = RecipeRouteService(
        sessions,
        publisher=AtomicRecipeRoutePublisher(publisher),
        prepare_key_routes=lambda runs: require_existing_recipe_routes(sessions, runs),
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
        inventory_max_age=INVENTORY_MAX_AGE_SECONDS,
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
            inventory_max_age=INVENTORY_MAX_AGE_SECONDS,
            disk_floor_bytes=10_000_000_000,
            compiled_plan_provider=compiled_plan_provider,
        ),
        run_admission=RunAdmissionService(
            sessions,
            inventory_max_age=INVENTORY_MAX_AGE_SECONDS,
            memory_floor_bytes=PLATFORM_MEMORY_FLOOR_BYTES,
        ),
        agent_jobs=agent_jobs,
        clock=clock,
        route_publications=recipe_routes,
        builds=recipe_builds,
        mappings=ClusterMappingService(sessions),
        distributed_start_timeout_seconds=distributed_start_timeout_seconds,
    )
    if model_cache is not None:
        model_cache._fleet_lifecycle = lifecycle
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
    # Builds, model downloads and sweeps end children here, not only reports.
    run_switch_operations.wake_on_child_end()
    fleet_profiles = build_production_fleet_profile_service(
        sessions,
        clock=clock,
        run_switch_operations=run_switch_operations,
        cache_resolver=(
            model_cache.resolve_latest_cached if model_cache is not None else None
        ),
    )
    residues = AttemptResidueReconciler(
        sessions,
        abandon_never_installed=lifecycle.abandon_never_installed,
        removal=lifecycle,
        clock=clock,
    )
    upgrades = AgentUpgradeService(sessions, agent_jobs, clock=clock)
    recipe_operations = RecipeOperationWorker(
        sessions,
        recipe_routes,
        clock=clock,
        reconcile_routes=False,
        order_reconcile=agent_jobs.reconcile_orders,
        build_cleanup=lifecycle.reconcile_cancelled_builds,
        retirement_cleanup=lifecycle.reconcile_retired_operations,
        stop_admission_cleanup=lifecycle.reconcile_pending_service_stops,
        residue_cleanup=residues.tick,
        fleet_profiles=fleet_profiles,
        run_switches=run_switch_operations,
        recoveries=DistributedRecoveryCoordinator(
            sessions,
            routes=recipe_routes,
            agent_jobs=agent_jobs,
            clock=clock,
            recovery_run_stops=lifecycle,
            run_endings=lifecycle,
            start_timeout_seconds=distributed_start_timeout_seconds,
        ),
    )
    worker_background_services = tuple(background_services)
    automatic_catalog = None
    if catalog_sync is not None:
        from ..catalog.sync import AutomaticCatalogSync
        from ..settings import RECIPE_LIBRARY_SYNC_INTERVAL_SECONDS

        # The one automatic catalog sync; the API only reads what it applied.
        automatic_catalog = AutomaticCatalogSync(
            catalog_sync, interval_seconds=RECIPE_LIBRARY_SYNC_INTERVAL_SECONDS
        )
        worker_background_services += (automatic_catalog.tick,)
    worker_background_closers = (*background_closers, upgrades.close)
    if automatic_catalog is not None:
        worker_background_closers += (automatic_catalog.close,)
    memory_sources: list[object] = [model_cache, artifact_phase_executor]
    close_artifact_executor = getattr(artifact_phase_executor, "close", None)
    if callable(close_artifact_executor):
        worker_background_closers += (close_artifact_executor,)
    prebuilt_importer = None
    image_production = None
    image_store_collector = None
    if image_cache_root is not None:
        from ..image_store_collection import ImageStoreCollector
        from ..prebuilt_images import PrebuiltImageImporter

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
        from ..availability_production.production import build_recipe_image_availability

        assert runtime_archive_storage is not None  # the root implies the cache

        image_production = build_recipe_image_availability(
            sessions,
            artifact_root=recipe_image_artifact_root,
            recipe_builds=recipe_builds,
            recipe_operations=lifecycle,
            model_cache=model_cache,
            clock=clock,
            max_parallel=recipe_image_parallel_preparations,
            with_scheduler=True,
            storage=runtime_archive_storage,
        )
        lifecycle.bind_install_preparation(image_production.service.prepare_install)
        assert image_production.scheduler is not None
        fleet_profiles.bind_preparation_starter(
            image_production.service.ensure_preparation,
            progress=image_production.service.preparation_progressed_at,
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
        images=runtime_archive_storage,
        model_cache_root=model_cache_root,
        model_cache=model_cache,
        image_blobs=image_store_collector,
        demands=storage_demands,
    )
    memory_sources += [storage_demands, storage_collector]
    fleet_profiles.bind_storage_relief(storage_collector.relief_for_spark)
    worker_background_services += (
        storage_collector.tick,
        recipe_builds.reconcile_source_bundles,
        CatalogRevisionCollector(sessions, clock=clock).tick,
        TerminalHistoryCollector(
            sessions, clock=clock, process_instance_id=process_instance_id
        ).tick,
    )
    if model_cache is not None:
        from ..model_cache.partials_collection import ModelPartialsCollector

        # An unfinished download is not a model file: its partials go a day
        # after the last live operation stopped writing them.
        worker_background_services += (
            ModelPartialsCollector(sessions, model_cache, clock=clock).tick,
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
    worker = Worker(
        jobs,
        worker_id,
        {},
        coordinator_failures=CoordinatorFailures(
            sessions, clock, _coordinator_source_content()
        ),
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
        step_heartbeat=step_heartbeat,
        memory_sources=memory_sources,
    )

    def unavailable(owner: WaitOwnerKind) -> None:
        raise UnknownOutcomeError(
            f"due wait owner capability unavailable: {owner}",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )

    def agent_order(key: RowKey, kind: WaitOwnerKind) -> object:
        adapter = AgentOperationAdapter(
            sessions=sessions,
            clock=clock,
            resume_candidates=operator_resume_candidates_in_session,
        )
        return _OrderStore(agent_jobs, adapter).redrive(str(key[0]))

    def images(key: RowKey, kind: WaitOwnerKind) -> object:
        if image_production is None or image_production.scheduler is None:
            return unavailable(kind)
        return image_production.scheduler.tick(str(key[0]))

    def prebuilt(key: RowKey, kind: WaitOwnerKind) -> object:
        if prebuilt_importer is None:
            return unavailable(kind)
        progressed = lifecycle.reconcile_cancelled_builds(str(key[0]))
        return prebuilt_importer.tick(str(key[0])) or progressed

    def recipe(key: RowKey, kind: WaitOwnerKind) -> object:
        progressed = lifecycle.redrive(str(key[0]))
        progressed = lifecycle.redrive_parent(str(key[0]), agent_jobs) or progressed
        # A persisted recipe build may itself own a Controller prebuilt pull.
        # Its normal importer decides from the current typed build identity.
        if prebuilt_importer is not None:
            return prebuilt_importer.tick(str(key[0])) or progressed
        return progressed

    def cache(key: RowKey, kind: WaitOwnerKind) -> object:
        if model_cache is None:
            return unavailable(kind)
        return model_cache.redrive(str(key[0]))

    def cleanup(key: RowKey, kind: WaitOwnerKind) -> object:
        progressed = residues.tick(str(key[0]))
        return storage_collector.redrive_installation(str(key[0])) or progressed

    def certificate(key: RowKey, kind: WaitOwnerKind) -> object:
        if enrollment is None:
            return unavailable(kind)
        return enrollment.reconcile_revocations(str(key[0]))

    def catalog(key: RowKey, kind: WaitOwnerKind) -> object:
        if automatic_catalog is None:
            return unavailable(kind)
        return automatic_catalog.tick()

    advancers: dict[WaitOwnerKind, AdvanceWait] = {
        WaitOwnerKind.COORDINATOR: lambda key, wait: worker.redrive_coordinator(
            str(key[0])
        ),
        WaitOwnerKind.AGENT_OPERATION: agent_order,
        WaitOwnerKind.AGENT_UPGRADE: lambda key, wait: upgrades.heal_rollouts(
            1, str(key[0])
        ),
        WaitOwnerKind.ARTIFACT_JOB: lambda key, wait: ArtifactJobAdapter(
            sessions=sessions, clock=clock
        ).redrive(str(key[0])),
        WaitOwnerKind.FLEET_PROFILE: lambda key, wait: fleet_profiles.tick(str(key[0])),
        WaitOwnerKind.IMAGE_AVAILABILITY: images,
        WaitOwnerKind.PREBUILT_IMAGE: prebuilt,
        WaitOwnerKind.JOB: lambda key, wait: worker._run_generic(str(key[0])),
        WaitOwnerKind.MODEL_CACHE: cache,
        WaitOwnerKind.RECIPE_OPERATION: recipe,
        WaitOwnerKind.RECIPE_UPDATE_BATCH: images,
        WaitOwnerKind.RUN_SWITCH: lambda key, wait: run_switch_operations.redrive(
            str(key[0])
        ),
        WaitOwnerKind.SELECTED_PROFILE: lambda key, wait: (
            fleet_profiles._reconcile_selected_roster(clock())
        ),
        WaitOwnerKind.ROUTE_PUBLICATION: lambda key, wait: recipe_routes.maintain(),
        WaitOwnerKind.ROUTE_RECOVERY: lambda key, wait: recipe_operations.redrive_run(
            str(key[0])
        ),
        WaitOwnerKind.STORAGE_RELIEF: lambda key, wait: (
            storage_collector.redrive_artifact(str(key[0]), str(key[1]))
        ),
        WaitOwnerKind.CLEANUP: cleanup,
        WaitOwnerKind.CATALOG_SYNC: catalog,
        WaitOwnerKind.CERTIFICATE: certificate,
        WaitOwnerKind.CERTIFICATE_REVOCATION: certificate,
    }
    backstop = DueWaitBackstop(sessions, wait_sources(advancers), clock=clock)
    worker._due_waits = backstop.tick
    backstop.timed = worker._timed
    recipe_operations.timed = worker._timed
    return worker
