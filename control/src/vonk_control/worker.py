"""Durable job worker entry point and bounded handler registry."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .jobs import JobService

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GENERATION = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_START_NONCE = re.compile(r"[0-9a-f]{64}\Z")
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_IMAGE = re.compile(r"[^\s]{1,1900}@sha256:[0-9a-f]{64}\Z")


class WorkerSelectedIdentityVerifier:
    """Freshly bind every worker heartbeat to the selected worker image."""

    def __init__(
        self,
        projections: Any,
        *,
        generation_id: str,
        release_digest: str,
        build_digest: str,
        platform_version: str,
        process_image: str,
        database_revision: str,
    ) -> None:
        if not callable(getattr(projections, "load_active", None)):
            raise TypeError("worker identity projection source is invalid")
        if (
            _GENERATION.fullmatch(generation_id) is None
            or _DIGEST.fullmatch(release_digest) is None
            or _DIGEST.fullmatch(build_digest) is None
            or _SEMVER.fullmatch(platform_version) is None
            or _IMAGE.fullmatch(process_image) is None
            or _GENERATION.fullmatch(database_revision) is None
        ):
            raise ValueError("worker selected identity is invalid")
        self._projections = projections
        self._expected = (
            ("projection_kind", "active", "projection kind"),
            ("generation_id", generation_id, "generation"),
            ("release_digest", release_digest, "release"),
            ("build_digest", build_digest, "build"),
            ("platform_version", platform_version, "platform version"),
            ("worker_image", process_image, "worker image"),
            ("database_revision", database_revision, "database revision"),
        )

    def verify(self) -> None:
        projection = self._projections.load_active()
        if projection is None:
            raise RuntimeError("active worker identity projection is unavailable")
        for field, expected, label in self._expected:
            actual = _selected_projection_field(projection, field)
            if actual != expected:
                raise RuntimeError(f"active worker {label} does not match process")


def _selected_projection_field(projection: object, field: str) -> object:
    if not isinstance(projection, Mapping):
        return getattr(projection, field, None)
    if field in projection:
        return projection[field]
    selection = projection.get("selection")
    if not isinstance(selection, Mapping):
        return None
    generation = selection.get("generation")
    if isinstance(generation, Mapping):
        return generation.get(field)
    return None


class WorkerHeartbeatRecorder:
    """Persist proof only after a real scheduler loop returns."""

    def __init__(
        self,
        sessions: Any,
        *,
        generation_id: str,
        release_digest: str,
        build_digest: str,
        start_nonce: str,
        clock: Callable[[], datetime],
        verify_selected: Callable[[], object] | None = None,
    ) -> None:
        if _GENERATION.fullmatch(generation_id) is None:
            raise ValueError("worker generation ID is invalid")
        if _DIGEST.fullmatch(release_digest) is None:
            raise ValueError("worker release digest is invalid")
        if _DIGEST.fullmatch(build_digest) is None:
            raise ValueError("worker build digest is invalid")
        if _START_NONCE.fullmatch(start_nonce) is None:
            raise ValueError("worker start nonce is invalid")
        self._sessions = sessions
        self._generation_id = generation_id
        self._release_digest = release_digest
        self._build_digest = build_digest
        self._start_nonce = start_nonce
        self._clock = clock
        self._verify_selected = verify_selected

    def completed_loop(self) -> None:
        from sqlalchemy import select

        from .models import ControlProcessHeartbeat

        if self._verify_selected is not None:
            self._verify_selected()
        completed_at = self._clock()
        if not isinstance(completed_at, datetime) or completed_at.tzinfo is None:
            raise ValueError("worker heartbeat clock must be timezone-aware")
        if completed_at.utcoffset() is None:
            raise ValueError("worker heartbeat clock must be timezone-aware")
        completed_at = completed_at.astimezone(UTC)
        with self._sessions.begin() as session:
            heartbeat = session.scalar(
                select(ControlProcessHeartbeat)
                .where(
                    ControlProcessHeartbeat.process_kind == "worker",
                    ControlProcessHeartbeat.start_nonce == self._start_nonce,
                )
                .with_for_update()
            )
            if heartbeat is None:
                session.add(
                    ControlProcessHeartbeat(
                        process_kind="worker",
                        generation_id=self._generation_id,
                        release_digest=self._release_digest,
                        build_digest=self._build_digest,
                        start_nonce=self._start_nonce,
                        loop_sequence=1,
                        completed_at=completed_at,
                    )
                )
                return
            if (
                heartbeat.generation_id != self._generation_id
                or heartbeat.release_digest != self._release_digest
                or heartbeat.build_digest != self._build_digest
            ):
                raise RuntimeError("worker heartbeat identity changed within a process")
            heartbeat.loop_sequence += 1
            heartbeat.completed_at = completed_at


@dataclass(frozen=True)
class HandlerRequest(Mapping[str, object]):
    job_id: str
    kind: str
    payload: Mapping[str, object]
    base_commit: str
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
        reconciliations=None,
        updates=None,
        recipes=None,
        quarantine_unlinked: bool = False,
        loop_heartbeat: Callable[[], object] | None = None,
    ) -> None:
        self._jobs = jobs
        self._worker_id = worker_id
        self._handlers = dict(handlers)
        self._logs = logs
        self._housekeeping = housekeeping
        self._reconciliations = reconciliations
        self._updates = updates
        self._recipes = recipes
        self._quarantine_unlinked = quarantine_unlinked
        self._loop_heartbeat = loop_heartbeat
        self._source_cursor = 0

    def run_once(self) -> bool:
        if self._housekeeping is not None:
            self._housekeeping()
        sources: list[Callable[[], bool]] = []
        if self._reconciliations is not None:
            sources.append(self._reconciliations.tick)
        if self._updates is not None:
            sources.append(self._updates.tick)
        if self._recipes is not None:
            sources.append(self._recipes.tick)
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

    def _run_generic(self) -> bool:
        if self._quarantine_unlinked:
            return self._jobs.quarantine_unlinked(
                "legacy unlinked job requires operator review"
            )
        attempt = self._jobs.claim(self._worker_id, 30)
        if attempt is None:
            return False
        if self._logs is not None:
            self._logs.save(attempt.job_id, f"job {attempt.kind} attempt {attempt.attempt} started".encode())
        handler = self._handlers.get(attempt.kind)
        if handler is None:
            self._jobs.fail(attempt, f"unsupported job kind: {attempt.kind}")
            if self._logs is not None:
                self._logs.save(attempt.job_id, b"job failed: unsupported job kind")
            return True
        try:
            result = handler(HandlerRequest(
                attempt.job_id, attempt.kind, attempt.payload,
                attempt.base_commit, attempt.targets,
            ))
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
            self._jobs.fail(attempt, f"{type(error).__name__}: {error}")
            if self._logs is not None:
                self._logs.save(attempt.job_id, f"job failed: {type(error).__name__}: {error}".encode())
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
    loop_heartbeat: Callable[[], object] | None = None,
) -> Worker:
    """Compose the one worker-owned reconciliation and platform-update runtime."""

    from .agent_reconciliation import AgentReconciliationService
    from .distributed_recovery import DistributedRecoveryCoordinator
    from .recipe_operation_worker import RecipeOperationWorker
    from .recipe_routes import AtomicRecipeRoutePublisher, RecipeRouteService
    from .telemetry_maintenance import (
        TelemetryMaintenance,
        TelemetryMaintenanceCadence,
    )
    from .update_routes import (
        ProductionUpdateRouteBoundary,
        load_authoritative_route_request,
    )
    from .update_worker import UpdateRolloutWorker
    from .updates import UpdateOrchestrator

    routes = ProductionUpdateRouteBoundary(
        sessions,
        publisher,
        route_root=route_root,
        request_loader=lambda session, reconciliation_id: (
            load_authoritative_route_request(
                session,
                reconciliation_id,
                endpoint_resolver=endpoint_resolver,
                clock=clock,
            )
        ),
        clock=clock,
    )
    update_orchestrator = UpdateOrchestrator(
        sessions,
        agent_jobs,
        routes,
        clock=clock,
    )
    updates = UpdateRolloutWorker(
        sessions,
        update_orchestrator,
        routes,
        authority,
    )
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
    recipe_operations = RecipeOperationWorker(
        sessions,
        recipe_routes,
        clock=clock,
        recoveries=DistributedRecoveryCoordinator(
            sessions,
            routes=recipe_routes,
            agent_jobs=agent_jobs,
            clock=clock,
        ),
    )
    telemetry_maintenance = TelemetryMaintenance(sessions, clock=clock)
    return Worker(
        jobs,
        worker_id,
        {},
        housekeeping=TelemetryMaintenanceCadence(
            telemetry_maintenance,
            clock=clock,
        ),
        reconciliations=reconciliations,
        updates=updates,
        recipes=recipe_operations,
        quarantine_unlinked=True,
        loop_heartbeat=loop_heartbeat,
    )


if __name__ == "__main__":
    import os
    import time
    from datetime import UTC, datetime
    from pathlib import Path

    from .agent_jobs import AgentJobService
    from .api import DirectoryIdentityProjectionSource
    from .db import build_engine, session_factory
    from .presence import AgentPresenceService, ManagementAddressPolicy
    from .route_runtime import (
        AtomicRouteBundlePublisher,
        FileSupervisorAcknowledger,
    )
    from .settings import GenerationStartupSettings, StartupMode, WorkerSettings
    from .update_signer import UnixUpdateSignerClient
    from .worker_authority import HttpWorkerAuthority

    settings = WorkerSettings.from_env_and_secrets()
    generation = GenerationStartupSettings.from_env_and_secrets()
    if generation.startup_mode is not StartupMode.SELECTED:
        raise RuntimeError("control worker requires selected startup mode")
    sessions = session_factory(build_engine(settings.database_url))
    clock = lambda: datetime.now(UTC)
    jobs = JobService(sessions, clock=clock)
    selected_identity = WorkerSelectedIdentityVerifier(
        DirectoryIdentityProjectionSource(generation.identity_root),
        generation_id=generation.generation_id,
        release_digest=generation.release_digest,
        build_digest=generation.build_digest,
        platform_version=generation.platform_version,
        process_image=generation.process_image,
        database_revision=generation.database_revision,
    )
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
        update_signer=UnixUpdateSignerClient(
            settings.update_signer_socket_path
        ),
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
        loop_heartbeat=WorkerHeartbeatRecorder(
            sessions,
            generation_id=generation.generation_id,
            release_digest=generation.release_digest,
            build_digest=generation.build_digest,
            start_nonce=generation.start_nonce,
            clock=clock,
            verify_selected=selected_identity.verify,
        ).completed_loop,
    )
    while True:
        if not worker.run_once():
            time.sleep(1)
