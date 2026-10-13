"""The world with parts the first simulation left out: the NAS model cache, the
agent order queue's claim and lease path, and distributed recovery.

Beside the services ``world`` composes, a ``ReachWorld`` runs the real
``ModelCacheService`` over its own NAS directory, the real artifact inspector
(NAS coverage comes from the cache, not a fake), the cache's download and
removal workers on every tick, the cache resolver Fleet profiles read, and the
unused-storage collector with the cache and a NAS disk whose free space the
simulation sets. The model's bytes come from a fake provider (huggingface.co
answered in memory) that can be down.

A load whose model is missing from the NAS plans a model download; the
download phase runs on the real cache through the production composite
executor. Spark transfers stay fake: they report the plan's own digests.

Every recipe names the same model, so a removal or eviction of it concerns
every load.

The worker runs the distributed-recovery coordinator beside the other owners,
as in production: a run whose rank a Spark reports gone is recovered or settled.

Orders go through the real agent order queue: the fake Sparks claim them,
renew their leases and report through it (``reach_sparks``), so a lost, late
or duplicate result meets the queue's fences, lease expiry and reissue.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import partial
from importlib import resources
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx2
from sqlalchemy import Engine, select
from vonk_control.agent_jobs import AgentJobService
from vonk_control.attempt_residues import AttemptResidueReconciler
from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
from vonk_control.distribution_executor import CompositeDistributionPhaseExecutor
from vonk_control.fleet_profiles import build_production_fleet_profile_service
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import CatalogDocumentHead, CatalogDocumentRevision
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.recipe_routes import RecipeRouteService
from vonk_control.run_switch_operations import PhaseExecution, RunSwitchOperationService
from vonk_control.storage_demands import StorageDemands
from vonk_control.unused_storage_collection import UnusedStorageCollector
from vonk_forge_contracts import document_sha256

from ..test_recipe_operations import ConcurrentPublisher
from ..test_run_switch_operations import RecordingArtifactExecutor, _fixture_execution
from .world import Controller, World, build_world

#: The model's one file, as the provider serves it.
WEIGHTS = bytes(range(256)) * 4
MODEL_REPOSITORY = "https://huggingface.co/vonk-forge/synthetic-tiny"
MODEL_SELECTOR = "vonk-forge/synthetic-tiny-fp16"
#: The NAS disk the collector reads: total, and free when it is not short.
NAS_TOTAL = 1 << 40
NAS_FREE = NAS_TOTAL // 2
#: Free NAS space while it is short: below the collector's low mark.
NAS_SHORT = NAS_TOTAL // 100
#: Removal and gate passes have a wall-clock budget; the fake clock owns time here.
_FROZEN_BUDGET = SimpleNamespace(monotonic=lambda: 0.0)


def _model_document() -> dict[str, Any]:
    document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    document["source"]["repository"] = MODEL_REPOSITORY
    (weights,) = document["files"]
    weights["sha256"] = hashlib.sha256(WEIGHTS).hexdigest()
    weights["size_bytes"] = len(WEIGHTS)
    return document


@dataclass
class Provider:
    """The model's upstream, answered in memory."""

    down_steps: int = 0
    requests: int = 0

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests += 1
        if self.down_steps:
            return httpx2.Response(503, request=request)
        if not request.url.path.endswith("/model.safetensors"):
            return httpx2.Response(404, request=request)
        start = 0
        requested = request.headers.get("range")
        if requested:
            start = int(requested.removeprefix("bytes=").split("-", 1)[0])
        body = WEIGHTS[start:]
        return httpx2.Response(
            206 if requested else 200,
            request=request,
            content=body,
            headers={"content-length": str(len(body))},
        )


class CacheArtifactExecutor:
    """Model downloads run on the real cache; Spark transfers stay fake.

    The fake verify reports the plan's own digests, which the real cache resolved.
    """

    def __init__(self, sessions, cache: ModelCacheService, clock) -> None:
        self.spark = RecordingArtifactExecutor()
        # No distribution service: the composite only runs model downloads here.
        self.nas = CompositeDistributionPhaseExecutor(
            sessions, None, None, model_cache=cache, clock=clock
        )

    def execute(self, plan, phase, **options) -> PhaseExecution:
        if phase.subphase == "model-download":
            return self.nas.execute(plan, phase, **options)
        if phase.kind == "verify":
            return _fixture_execution(
                plan,
                phase,
                result={
                    "verified": True,
                    "verified_digests": list(plan.storage.artifact_digests),
                    "verified_build_id": getattr(plan, "recipe_build_id", None),
                    "verified_image_digest": plan.image_digest,
                    "verified_oci_layout_sha256": plan.build.oci_layout_sha256,
                },
            )
        return self.spark.execute(plan, phase, **options)

    def get(self, operation_id: str):
        if operation_id in self.spark.children:
            return self.spark.get(operation_id)
        return self.nas.get(operation_id)

    def abandon(self, session, operation_id: str, now, *, reason: str) -> bool:
        return self.spark.abandon(session, operation_id, now, reason=reason)

    def close(self) -> None:
        self.nas.close()


@dataclass
class ReachController(Controller):
    cache: ModelCacheService
    artifacts: CacheArtifactExecutor
    orders: AgentJobService


@dataclass
class ReachWorld(World):
    """``World`` with the NAS model cache and its provider."""

    root: Path | None = None
    provider: Provider = field(default_factory=Provider)
    #: Steps the NAS stays short of disk.
    nas_short_steps: int = 0
    #: The Controller was paused (the clock jumped) since the last round.
    paused: bool = False

    def __post_init__(self) -> None:
        self._add_heads()
        super().__post_init__()

    @property
    def reach(self) -> ReachController:
        assert isinstance(self.controller, ReachController)
        return self.controller

    def close(self) -> None:
        self._stop_controller()
        super().close()

    def restart(self) -> None:
        self._stop_controller()
        super().restart()

    def tick(self) -> None:
        super().tick()
        try:
            with patch(
                "vonk_control.model_cache.removal_reconcile.time", _FROZEN_BUDGET
            ):
                self.reach.cache.run_pending(limit=4)
        except Exception as error:  # noqa: BLE001 - the worker contains it; the invariant reports it
            self.raised_ticks.raised.append(
                f"model cache: {type(error).__name__}: {error}"
            )

    def nas_disk(self, _path: Path) -> tuple[int, int]:
        return NAS_TOTAL, NAS_SHORT if self.nas_short_steps else NAS_FREE

    def _stop_controller(self) -> None:
        controller = getattr(self, "controller", None)
        if isinstance(controller, ReachController):
            controller.artifacts.close()
            controller.cache.close()

    def _add_heads(self) -> None:
        """The catalog's active revision of every document, as a sync leaves it."""

        with self.sessions.begin() as session:
            for row in session.scalars(
                select(CatalogDocumentRevision).order_by(
                    CatalogDocumentRevision.revision_number
                )
            ):
                head = session.scalar(
                    select(CatalogDocumentHead).where(
                        CatalogDocumentHead.kind == row.kind,
                        CatalogDocumentHead.publisher == row.publisher,
                        CatalogDocumentHead.slug == row.slug,
                    )
                )
                if head is None:
                    session.add(
                        CatalogDocumentHead(
                            kind=row.kind,
                            publisher=row.publisher,
                            slug=row.slug,
                            active_revision_id=row.id,
                        )
                    )
                else:
                    head.active_revision_id = row.id

    def _start(self) -> Controller:
        assert self.root is not None
        clock = self.clock
        template = self.template
        cache = ModelCacheService(
            self.sessions,
            self.root / "nas-cache",
            reserve_bytes=0,
            clock=clock,
            http_client=httpx2.Client(
                transport=httpx2.MockTransport(self.provider.handle),
                follow_redirects=False,
            ),
        )
        # Installations compile against what the NAS really holds.
        compile_installation = template._install_admission._compiled_plan_provider
        assert compile_installation is not None
        compile_installation.__self__._model_cache = cache
        orders = AgentJobService(self.sessions, clock=clock)
        routes = RecipeRouteService(
            self.sessions,
            publisher=ConcurrentPublisher(),
            management_policy=ManagementAddressPolicy.parse("192.168.1.0/24"),
            clock=clock,
        )
        lifecycle = RecipeOperationService(
            self.sessions,
            install_admission=template._install_admission,
            run_admission=template._run_admission,
            agent_jobs=orders,
            clock=clock,
            route_publications=routes,
        )
        orders.set_result_consumer(lifecycle.consume_agent_result)
        artifacts = CacheArtifactExecutor(self.sessions, cache, clock)
        planner = RunSwitchOperationService(
            self.sessions,
            lifecycle=lifecycle,
            clock=clock,
            model_cache=cache,
            artifact_phase_executor=artifacts,
            memory_floor_bytes=50,
        )
        profiles = build_production_fleet_profile_service(
            self.sessions,
            clock=clock,
            run_switch_operations=planner,
            cache_resolver=cache.resolve_latest_cached,
        )
        demands = StorageDemands(clock)
        lifecycle.bind_storage_demands(demands)
        cache.bind_storage_demands(demands)
        storage = UnusedStorageCollector(
            self.sessions,
            clock=clock,
            lifecycle=lifecycle,
            model_cache=cache,
            model_cache_root=cache.root,
            demands=demands,
            disk_usage=self.nas_disk,
        )
        profiles.bind_storage_relief(storage.relief_for_spark)
        worker = RecipeOperationWorker(
            self.sessions,
            routes,
            clock=clock,
            order_reconcile=orders.reconcile_orders,
            stop_admission_cleanup=lifecycle.reconcile_pending_service_stops,
            build_cleanup=lifecycle.reconcile_cancelled_builds,
            retirement_cleanup=lifecycle.reconcile_retired_operations,
            residue_cleanup=AttemptResidueReconciler(
                self.sessions,
                abandon_never_installed=lifecycle.abandon_never_installed,
                removal=lifecycle,
                clock=clock,
            ).tick,
            fleet_profiles=profiles,
            run_switches=planner,
            recoveries=DistributedRecoveryCoordinator(
                self.sessions,
                routes=routes,
                agent_jobs=orders,
                clock=clock,
                recovery_run_stops=lifecycle,
            ),
        )
        return ReachController(
            lifecycle, planner, profiles, worker, storage, cache, artifacts, orders
        )


def build_reach_world(tmp_path: Path, engine: Engine) -> ReachWorld:
    model = _model_document()
    digest = document_sha256(model)

    def bind_model(recipe: dict[str, Any]) -> None:
        recipe["models"][0]["model"]["content_sha256"] = digest

    world = build_world(
        tmp_path,
        engine,
        world_type=partial(ReachWorld, root=tmp_path / "reach"),
        model_transform=lambda document: document.update(model),
        recipe_transform=bind_model,
    )
    assert isinstance(world, ReachWorld)
    return world
