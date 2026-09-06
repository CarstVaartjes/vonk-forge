"""Durable job worker entry point and bounded handler registry."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .jobs import JobService

_PROCESS_INSTANCE = re.compile(r"[0-9a-f]{64}\Z")


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

        from .models import ControlProcessHeartbeat

        with self._sessions.begin() as session:
            heartbeat = session.scalar(
                select(ControlProcessHeartbeat)
                .where(ControlProcessHeartbeat.process_kind == "worker")
                .with_for_update()
            )
            if heartbeat is None:
                session.add(
                    ControlProcessHeartbeat(
                        process_kind="worker",
                        process_instance_id=self._process_instance_id,
                        loop_sequence=0,
                        completed_at=None,
                    )
                )
                return
            heartbeat.process_instance_id = self._process_instance_id
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
        logs=None,
        housekeeping: Callable[[], object] | None = None,
        artifact_housekeeping: Callable[[], object] | None = None,
        reconciliations=None,
        recipes=None,
        model_cache=None,
        loop_heartbeat: Callable[[], object] | None = None,
    ) -> None:
        self._jobs = jobs
        self._worker_id = worker_id
        self._handlers = dict(handlers)
        self._logs = logs
        self._housekeeping = housekeeping
        self._artifact_housekeeping = artifact_housekeeping
        self._reconciliations = reconciliations
        self._recipes = recipes
        self._model_cache = model_cache
        self._loop_heartbeat = loop_heartbeat
        self._source_cursor = 0

    def run_once(self) -> bool:
        if self._housekeeping is not None:
            self._housekeeping()
        if self._artifact_housekeeping is not None:
            self._artifact_housekeeping()
        sources: list[Callable[[], bool]] = []
        if self._reconciliations is not None:
            sources.append(self._reconciliations.tick)
        if self._recipes is not None:
            sources.append(self._recipes.tick)
        if self._model_cache is not None:
            sources.append(self._run_model_cache)
        sources.append(self._run_generic)
        if self._source_cursor >= len(sources):
            self._source_cursor = 0
        advanced = False
        for offset in range(len(sources)):
            index = (self._source_cursor + offset) % len(sources)
            if sources[index]():
                self._source_cursor = (index + 1) % len(sources)
                advanced = True
                break
        if self._loop_heartbeat is not None:
            self._loop_heartbeat()
        return advanced

    def _run_model_cache(self) -> bool:
        return bool(self._model_cache.run_pending(limit=1))

    def _run_generic(self) -> bool:
        attempt = self._jobs.claim(self._worker_id, 30)
        if attempt is None:
            return False
        if self._logs is not None:
            self._logs.save(
                attempt.job_id,
                f"job {attempt.kind} attempt {attempt.attempt} started".encode(),
            )
        handler = self._handlers.get(attempt.kind)
        if handler is None:
            self._jobs.fail(attempt, f"unsupported job kind: {attempt.kind}")
            if self._logs is not None:
                self._logs.save(attempt.job_id, b"job failed: unsupported job kind")
            return True
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
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
            self._jobs.fail(attempt, f"{type(error).__name__}: {error}")
            if self._logs is not None:
                self._logs.save(
                    attempt.job_id,
                    f"job failed: {type(error).__name__}: {error}".encode(),
                )
        else:
            self._jobs.succeed(attempt, result)
            if self._logs is not None:
                self._logs.save(attempt.job_id, b"job succeeded")
        return True


def assemble_production_worker(
    *,
    jobs,
    sessions,
    agent_jobs,
    publisher,
    route_root,
    endpoint_resolver,
    management_policy,
    clock,
    authority,
    worker_id: str,
    artifact_job_root: Path,
    artifact_job_storage_max_bytes: int,
    artifact_job_retention_seconds: int,
    artifact_job_reconcile_interval_seconds: int,
    artifact_job_reconcile_batch_limit: int,
    model_cache=None,
    agent_artifact_root: Path | None = None,
    operator_jurisdiction: str | None = None,
    compiled_plan_provider: Callable[..., Mapping[str, Mapping[str, object]]] | None = None,
    runtime_image_preparer: Callable[..., object] | None = None,
    loop_heartbeat: Callable[[], object] | None = None,
) -> Worker:
    """Compose the worker-owned reconciliation runtime."""

    from .agent_reconciliation import AgentReconciliationService
    from .artifact_blob_store import ArtifactBlobStore
    from .artifact_jobs import ArtifactJobService
    from .artifact_maintenance import ArtifactMaintenanceCadence
    from .artifact_sizes import DeclaredArtifactSizeResolver
    from .cluster_mappings import ClusterMappingService
    from .distributed_recovery import DistributedRecoveryCoordinator
    from .distribution import build_distribution_service_from_components
    from .distribution_executor import CompositeDistributionPhaseExecutor
    from .fleet_profiles import FleetProfileService, RunSwitchFleetProfileAdapter
    from .install_admission import InstallAdmissionService
    from .recipe_builds import RecipeBuildService
    from .recipe_operation_worker import RecipeOperationWorker
    from .recipe_operations import RecipeOperationService
    from .recipe_routes import AtomicRecipeRoutePublisher, RecipeRouteService
    from .run_admission import RunAdmissionService
    from .run_switch_operations import RunSwitchOperationService
    from .source_bundles import DatabaseSourceBundleStore
    from .telemetry_maintenance import (
        TelemetryMaintenance,
        TelemetryMaintenanceCadence,
    )

    if model_cache is not None:
        if agent_artifact_root is None:
            raise ValueError("agent artifact root is required with model cache")
        distribution = build_distribution_service_from_components(
            model_cache,
            sessions,
            agent_artifact_root,
            clock=clock,
        )
        artifact_phase_executor = CompositeDistributionPhaseExecutor(
            sessions,
            agent_jobs,
            distribution,
            model_cache=model_cache,
            runtime_image_preparer=runtime_image_preparer,
            clock=clock,
        )
    else:
        artifact_phase_executor = None

    reconciliations = AgentReconciliationService(
        sessions,
        agent_jobs=agent_jobs,
        publisher=publisher,
        endpoint_resolver=endpoint_resolver,
        clock=clock,
        authority_prefetch=authority.prefetch,
        authority_check=authority.authorization_reason,
        authority_clear=authority.clear,
    )
    recipe_routes = RecipeRouteService(
        sessions,
        publisher=AtomicRecipeRoutePublisher(publisher, clock=clock),
        management_policy=management_policy,
        clock=clock,
        maximum_age_seconds=120,
    )
    lifecycle = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(
            sessions,
            sizes=DeclaredArtifactSizeResolver(),
            inventory_max_age=300,
            disk_floor_bytes=10_000_000_000,
            operator_jurisdiction=operator_jurisdiction,
            compiled_plan_provider=compiled_plan_provider,
        ),
        run_admission=RunAdmissionService(
            sessions,
            inventory_max_age=300,
            memory_floor_bytes=4_000_000_000,
            operator_jurisdiction=operator_jurisdiction,
        ),
        agent_jobs=agent_jobs,
        clock=clock,
        route_publications=recipe_routes,
        builds=RecipeBuildService(
            sessions,
            bundles=DatabaseSourceBundleStore(sessions),
            inventory_max_age=300,
        ),
        mappings=ClusterMappingService(sessions),
    )
    run_switch_operations = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=clock,
        mappings=ClusterMappingService(sessions),
        model_cache=model_cache,
        artifact_phase_executor=artifact_phase_executor,
    )
    recipe_operations = RecipeOperationWorker(
        sessions,
        recipe_routes,
        clock=clock,
        fleet_profiles=FleetProfileService(
            sessions,
            clock=clock,
            recipe_operations=lifecycle,
            switch_adapter=RunSwitchFleetProfileAdapter(
                sessions, run_switch_operations
            ),
        ),
        run_switches=run_switch_operations,
        recoveries=DistributedRecoveryCoordinator(
            sessions,
            routes=recipe_routes,
            agent_jobs=agent_jobs,
            clock=clock,
        ),
    )
    telemetry_maintenance = TelemetryMaintenance(sessions, clock=clock)
    artifact_jobs = ArtifactJobService(
        sessions,
        recipe_operations=lifecycle,
        blob_store=ArtifactBlobStore(
            artifact_job_root,
            max_stored_bytes=artifact_job_storage_max_bytes,
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
        reconciliations=reconciliations,
        recipes=recipe_operations,
        model_cache=model_cache,
        loop_heartbeat=loop_heartbeat,
    )


if __name__ == "__main__":
    import os
    import time
    from datetime import UTC, datetime
    from pathlib import Path

    from sqlalchemy import select
    from vonk_forge_contracts import RecipeDefinition, content_sha256

    from .agent_jobs import AgentJobService
    from .db import build_engine, session_factory, wait_for_database
    from .execution_plan_service import ControllerExecutionPlanService
    from .model_cache import ModelCacheService
    from .models import CatalogDocumentRevision
    from .presence import AgentPresenceService, ManagementAddressPolicy
    from .route_runtime import (
        AtomicRouteBundlePublisher,
        FileSupervisorAcknowledger,
    )
    from .runtime_image_preparation import (
        FilesystemRuntimeImageStorage,
        SkopeoOCIImageTransport,
        persist_runtime_image_receipt,
        prepare_runtime_image,
        resolve_persisted_runtime_image_receipt,
    )
    from .settings import WorkerSettings
    from .worker_authority import HttpWorkerAuthority

    settings = WorkerSettings.from_env_and_secrets()
    wait_for_database(settings.database_url)
    sessions = session_factory(build_engine(settings.database_url))
    clock = lambda: datetime.now(UTC)
    jobs = JobService(sessions, clock=clock)
    address_policy = ManagementAddressPolicy.parse(
        settings.management_cidrs,
        forbidden_cidrs=settings.direct_fabric_cidrs,
    )
    presence = AgentPresenceService(sessions, address_policy, clock=clock)

    def endpoint(session, node_id: str) -> tuple[str, datetime]:
        observation = presence.latest_in_session(
            session, node_id, maximum_age_seconds=300
        )
        return observation.address, observation.observed_at

    authority = HttpWorkerAuthority(
        settings.internal_api_url,
        settings.internal_api_token,
        timeout_seconds=settings.internal_api_timeout_seconds,
    )
    agent_jobs = AgentJobService(
        sessions,
        clock=clock,
    )
    route_root = Path("/routes")
    publisher = AtomicRouteBundlePublisher(
        route_root,
        management_policy=address_policy,
        clock=clock,
        maximum_lease_seconds=300,
        await_supervisor_ack=FileSupervisorAcknowledger(
            Path("/supervisor/ack.json"),
            clock=clock,
        ),
    )
    model_cache = ModelCacheService(
        sessions,
        settings.model_cache_root,
        reserve_bytes=settings.model_cache_reserve_bytes,
        clock=clock,
        huggingface_token_path=settings.huggingface_token_path,
    )
    model_cache.resume_operations()
    runtime_image_storage = FilesystemRuntimeImageStorage(
        settings.agent_artifact_root
    )
    runtime_image_transport = SkopeoOCIImageTransport()

    def prepare_runtime_image_receipt(document, runtime_spec, build):
        runtime = runtime_spec.get("runtime")
        if not isinstance(runtime, Mapping):
            raise TypeError("compiled runtime projection is unavailable")
        build_receipt = None
        execution = document.get("execution")
        if isinstance(execution, Mapping) and execution.get("mode") == "build":
            if build is None:
                raise ValueError("source build receipt is unavailable")
            build_receipt = {
                "state": build.state,
                "build_id": build.id,
                "image_digest": build.image_digest,
                "oci_layout_sha256": build.oci_layout_sha256,
                "image_bytes": build.image_bytes,
            }
        identity = runtime_spec.get("identity")
        effective_execution_key = (
            identity.get("execution_sha256")
            if isinstance(identity, Mapping)
            else None
        )
        if not isinstance(effective_execution_key, str):
            raise TypeError("compiled runtime execution identity is unavailable")

        def write_receipt(receipt):
            recipe_digest = content_sha256(RecipeDefinition.model_validate(document))
            with sessions.begin() as session:
                revision = session.scalar(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.state == "active",
                        CatalogDocumentRevision.content_digest == recipe_digest,
                    )
                )
                if revision is None or revision.content_digest is None:
                    raise ValueError("active recipe revision for runtime receipt is unavailable")
                persist_runtime_image_receipt(
                    session,
                    recipe_revision_id=revision.id,
                    original_content_digest=revision.content_digest,
                    effective_execution_key=effective_execution_key,
                    receipt=receipt,
                    verified_at=clock(),
                )

        return prepare_runtime_image(
            document,
            runtime=runtime,
            storage=runtime_image_storage,
            transport=runtime_image_transport,
            build_receipt=build_receipt,
            now=clock(),
            receipt_writer=write_receipt,
        )

    def resolve_runtime_image_receipt(document, image_digest, runtime_spec):
        runtime = runtime_spec.get("runtime") if isinstance(runtime_spec, Mapping) else None
        if not isinstance(runtime, Mapping):
            raise TypeError("runtime image preparation is required: runtime projection is unavailable")
        architecture = runtime.get("architecture")
        interface = runtime.get("interface", runtime.get("runtime_interface"))
        if not isinstance(architecture, str) or not isinstance(interface, str):
            raise TypeError("runtime image preparation is required: platform identity is unavailable")
        receipt = runtime_image_storage.find_verified(
            image_digest,
            expected_architecture=architecture,
            expected_runtime_interface=interface,
        )
        if receipt is None:
            raise ValueError("runtime image preparation is required before compile/install")
        identity = runtime_spec.get("identity")
        execution_key = identity.get("execution_sha256") if isinstance(identity, Mapping) else None
        recipe_digest = content_sha256(RecipeDefinition.model_validate(document))
        if not isinstance(execution_key, str):
            raise TypeError("runtime image preparation execution identity is unavailable")
        with sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                    CatalogDocumentRevision.content_digest == recipe_digest,
                )
            )
            if revision is None:
                raise ValueError("durable runtime image receipt is unavailable before compile/install")
            resolve_persisted_runtime_image_receipt(
                session,
                recipe_revision_id=revision.id,
                original_content_digest=recipe_digest,
                effective_execution_key=execution_key,
                receipt=receipt,
            )
        if revision is None:
            raise ValueError("durable runtime image receipt is unavailable before compile/install")
        return receipt

    execution_plans = ControllerExecutionPlanService(
        model_cache,
        runtime_image_resolver=resolve_runtime_image_receipt,
    )
    worker = assemble_production_worker(
        jobs=jobs,
        sessions=sessions,
        agent_jobs=agent_jobs,
        publisher=publisher,
        route_root=route_root,
        endpoint_resolver=endpoint,
        management_policy=address_policy,
        clock=clock,
        authority=authority,
        worker_id=os.environ.get("HOSTNAME", "control-worker"),
        operator_jurisdiction=settings.operator_jurisdiction,
        artifact_job_root=settings.state_path / "artifact-jobs" / "blobs",
        artifact_job_storage_max_bytes=settings.artifact_job_storage_max_bytes,
        artifact_job_retention_seconds=settings.artifact_job_retention_seconds,
        artifact_job_reconcile_interval_seconds=(
            settings.artifact_job_reconcile_interval_seconds
        ),
        artifact_job_reconcile_batch_limit=settings.artifact_job_reconcile_batch_limit,
        model_cache=model_cache,
        agent_artifact_root=settings.agent_artifact_root,
        compiled_plan_provider=execution_plans.compile_installation,
        runtime_image_preparer=prepare_runtime_image_receipt,
        loop_heartbeat=WorkerHeartbeatRecorder(
            sessions,
            process_instance_id=current_worker_instance_id(),
            clock=clock,
        ).completed_loop,
    )
    while True:
        if not worker.run_once():
            time.sleep(1)
