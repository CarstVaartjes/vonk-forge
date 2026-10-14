"""Two Sparks and the real Controller services over one database.

The services are the ones the production worker composes
(``assemble_production_worker``): the recipe lifecycle, the Run/Switch planner,
Fleet profiles, the recipe operation worker with its order, stop, retirement
and residue reconcilers, and the unused-storage collector. They run over a test
database with a fake clock. A Controller restart builds them again over the same
database: in-memory state and retry schedules are gone, durable state is not.
"""

from __future__ import annotations

import copy
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.agent_jobs import AgentJobService
from vonk_control.attempt_residues import AttemptResidueReconciler
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import (
    FleetProfileService,
    build_production_fleet_profile_service,
)
from vonk_control.models import (
    CatalogDocument,
    CatalogDocumentRevision,
    RecipeBuild,
)
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.recipe_routes import RecipeRouteService
from vonk_control.run_switch_operations import RunSwitchOperationService
from vonk_control.storage_demands import StorageDemands
from vonk_control.stored_json import write_guard_mode
from vonk_control.unused_storage_collection import UnusedStorageCollector
from vonk_forge_contracts import RecipeDefinition, document_sha256

from ..test_recipe_operations import ConcurrentPublisher, setup_services
from ..test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)

#: The recipe that needs both Sparks, and two different ones that run on
#: either Spark alone (so one load can place two recipes on one Spark).
PAIR = "pair"
SOLO = "solo"
SOLO_B = "solo-b"
_SINGLE_SPARK = {SOLO: "qwen3-solo", SOLO_B: "qwen3-solo-b"}
#: What the recipe operation worker logs when one of its owners raised.
_WORKER_LOGGER = "vonk-control-worker"
_OWNER_FAILED = "recipe coordinator observation failed"


class _RaisedTicks(logging.Handler):
    """Collects every owner tick the worker had to contain."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.raised: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() != _OWNER_FAILED:
            return
        owner = getattr(record, "owner", "unknown owner")
        error = record.exc_info[1] if record.exc_info else None
        self.raised.append(f"{owner}: {type(error).__name__}: {error}")


@dataclass
class Controller:
    """One Controller worker process: its services and in-memory state."""

    lifecycle: RecipeOperationService
    planner: RunSwitchOperationService
    profiles: FleetProfileService
    worker: RecipeOperationWorker
    storage: UnusedStorageCollector


@dataclass
class World:
    """The shared database, the fake clock and the current Controller."""

    sessions: sessionmaker[Session]
    nodes: tuple[str, ...]
    recipes: dict[str, str]
    now: list[datetime]
    template: RecipeOperationService
    #: Each recipe's readable document, kept beside the database so a successor
    #: can still be published after a stored revision became unreadable.
    readable: dict[str, dict[str, Any]]
    controller: Controller = field(init=False)
    raised_ticks: _RaisedTicks = field(default_factory=_RaisedTicks)
    #: The world's own engine, released when it closes.
    engine: Engine | None = None

    def __post_init__(self) -> None:
        self.controller = self._start()
        logging.getLogger(_WORKER_LOGGER).addHandler(self.raised_ticks)

    def close(self) -> None:
        logging.getLogger(_WORKER_LOGGER).removeHandler(self.raised_ticks)
        if self.engine is not None:
            self.engine.dispose()

    def clock(self) -> datetime:
        return self.now[0]

    def advance(self, seconds: float) -> None:
        self.now[0] += timedelta(seconds=seconds)

    def restart(self) -> None:
        """A new worker process over the same database and clock."""

        self.controller = self._start()

    def tick(self) -> None:
        """One pass of the worker loop over the recipe owners and storage."""

        self.controller.worker.tick()
        try:
            self.controller.storage.tick()
        except Exception as error:  # noqa: BLE001 - the worker contains it; the invariant reports it
            self.raised_ticks.raised.append(
                f"unused storage collection: {type(error).__name__}: {error}"
            )

    def _start(self) -> Controller:
        clock = self.clock
        template = self.template
        lifecycle = RecipeOperationService(
            self.sessions,
            install_admission=template._install_admission,
            run_admission=template._run_admission,
            agent_jobs=template._agent_jobs,
            clock=clock,
        )
        planner = RunSwitchOperationService(
            self.sessions,
            lifecycle=lifecycle,
            clock=clock,
            artifacts=CompleteArtifactInspector(),
            artifact_phase_executor=RecordingArtifactExecutor(),
            memory_floor_bytes=50,
        )
        profiles = build_production_fleet_profile_service(
            self.sessions, clock=clock, run_switch_operations=planner
        )
        orders = AgentJobService(
            self.sessions, clock=clock, result_consumer=lifecycle.consume_agent_result
        )
        demands = StorageDemands(clock)
        lifecycle.bind_storage_demands(demands)
        storage = UnusedStorageCollector(
            self.sessions, clock=clock, lifecycle=lifecycle, demands=demands
        )
        profiles.bind_storage_relief(storage.relief_for_spark)
        worker = RecipeOperationWorker(
            self.sessions,
            RecipeRouteService(
                self.sessions,
                publisher=ConcurrentPublisher(),
                management_policy=ManagementAddressPolicy.parse("192.168.1.0/24"),
                clock=clock,
            ),
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
        )
        return Controller(lifecycle, planner, profiles, worker, storage)

    def create_profile(
        self, name: str, assignments: list[dict[str, object]]
    ) -> tuple[str, int]:
        """A saved profile's id and number."""

        profile = self.controller.profiles.create(
            FleetProfileInput.model_validate(
                {"name": name, "assignments": assignments}
            ),
            actor="admin",
        )
        return profile.id, profile.number

    def assignment(self, recipe: str, nodes: tuple[str, ...]) -> dict[str, object]:
        return {
            "recipe_selector": f"vonk-forge/{self.recipes[recipe]}",
            "spark_ids": list(nodes),
            "desired_state": "running",
            "assignment_name": f"{recipe}-{len(nodes)}",
        }

    def make_unreadable(self, recipe: str, *, successor: bool = True) -> None:
        """The contract moved on: ``recipe``'s stored revisions no longer parse,
        and (with ``successor``) the catalog publishes one the current contract
        accepts."""

        with self.sessions.begin() as session:
            stored = session.execute(
                select(
                    CatalogDocumentRevision.id, CatalogDocumentRevision.document
                ).where(CatalogDocumentRevision.slug == self.recipes[recipe])
            ).all()
        # The bytes are intact (the digest matches them); only the parser no
        # longer accepts them.
        with write_guard_mode(strict=False), self.sessions.begin() as session:
            for revision_id, document in stored:
                damaged = {
                    key: value for key, value in document.items() if key != "runtime"
                }
                session.execute(
                    update(CatalogDocumentRevision)
                    .where(CatalogDocumentRevision.id == revision_id)
                    .values(document=damaged, content_digest=document_sha256(damaged))
                    .execution_options(synchronize_session=False)
                )
        if successor:
            self.publish_successor(recipe)

    def publish_successor(self, recipe: str, *, peak_bytes: int | None = None) -> str:
        """The catalog publishes a newer revision of ``recipe`` with the same image.

        ``peak_bytes`` is the memory its roles declare, when it changes.
        """

        with self.sessions.begin() as session:
            latest = session.execute(
                select(
                    CatalogDocumentRevision.document_id,
                    CatalogDocumentRevision.revision_number,
                )
                .where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.slug == self.recipes[recipe],
                )
                .order_by(CatalogDocumentRevision.revision_number.desc())
                .limit(1)
            ).one_or_none()
            build = session.scalar(
                select(RecipeBuild)
                .where(RecipeBuild.state == "succeeded")
                .order_by(RecipeBuild.created_at, RecipeBuild.id)
                .limit(1)
            )
            assert latest is not None and build is not None
            document = _editable(self.readable[recipe])
            document["metadata"]["description"] = (
                f"Revision {latest.revision_number + 1}"
            )
            if peak_bytes is not None:
                for role in document["topology"]["roles"]:
                    role["resources"]["memory"]["peak_bytes"] = peak_bytes
            revision = _add_revision(
                session,
                document,
                document_id=latest.document_id,
                revision_number=latest.revision_number + 1,
                now=self.clock(),
            )
            _add_build(session, build, revision, now=self.clock())
            return revision.id


def build_world(tmp_path: Path, engine: Engine) -> World:
    # The test's database is fresh and lives as long as the world, so a pooled
    # connection never goes stale: no liveness ping before every session (a
    # round opens some fifty sessions; the pings were a third of its round trips).
    engine = create_engine(engine.url, pool_pre_ping=False)
    sessions, lifecycle, _queue, _mapping, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=engine
    )
    now = [lifecycle._clock()]
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        pair = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert pair is not None
        recipes = {PAIR: pair.slug}
        readable = {PAIR: _editable(pair.document)}
        for recipe, slug in _SINGLE_SPARK.items():
            document = _single_spark_document(pair.document, slug)
            catalog = CatalogDocument(
                kind="recipe",
                publisher=pair.publisher,
                slug=slug,
                title=document["metadata"]["title"],
                created_by="admin",
                created_at=now[0],
                updated_at=now[0],
            )
            session.add(catalog)
            session.flush()
            revision = _add_revision(
                session, document, document_id=catalog.id, revision_number=1, now=now[0]
            )
            _add_build(session, build, revision, now=now[0])
            recipes[recipe] = slug
            readable[recipe] = _editable(revision.document)
    return World(
        sessions=sessions,
        nodes=nodes,
        recipes=recipes,
        now=now,
        template=lifecycle,
        readable=readable,
        engine=engine,
    )


def _editable(document: object) -> dict[str, Any]:
    """A deep copy of a stored JSON document this test may edit freely."""

    copied = copy.deepcopy(document)
    assert isinstance(copied, dict)
    return copied


def _single_spark_document(pair: object, slug: str) -> dict[str, Any]:
    """The pair recipe reduced to its endpoint role on a single Spark."""

    document = _editable(pair)
    document["identity"]["slug"] = slug
    document["metadata"]["title"] = f"{slug} on one Spark"
    topology = document["topology"]
    (entrypoint,) = (role for role in topology["roles"] if role["endpoint_owner"])
    entrypoint = {**entrypoint, "count": 1}
    document["topology"] = {
        **topology,
        "name": "single",
        "node_count": 1,
        "parallelism": {**topology["parallelism"], "tensor": 1},
        "roles": [entrypoint],
        "start_order": [entrypoint["name"]],
    }
    for item in document["models"][0]["files"]:
        item["roles"] = [entrypoint["name"]]
    return RecipeDefinition.model_validate(document).model_dump(mode="json")


def _add_revision(
    session: Session,
    document: dict[str, Any],
    *,
    document_id: str,
    revision_number: int,
    now: datetime,
) -> CatalogDocumentRevision:
    canonical = RecipeDefinition.model_validate(document).model_dump(mode="json")
    revision = CatalogDocumentRevision(
        id=str(uuid.uuid4()),
        document_id=document_id,
        kind="recipe",
        publisher=canonical["identity"]["publisher"],
        slug=canonical["identity"]["slug"],
        revision_number=revision_number,
        schema_version=2,
        state="active",
        document=canonical,
        content_digest=document_sha256(canonical),
        projected={},
        created_by="admin",
        created_at=now,
    )
    session.add(revision)
    session.flush()
    return revision


def _add_build(
    session: Session,
    template: RecipeBuild,
    revision: CatalogDocumentRevision,
    *,
    now: datetime,
) -> None:
    """The succeeded build of ``revision``: the template's image, as its own receipt."""

    build = RecipeBuild(
        recipe_revision_id=revision.id,
        builder_node_id=template.builder_node_id,
        source_bundle_sha256=template.source_bundle_sha256,
        build_input_sha256=template.build_input_sha256,
        state="succeeded",
        policy_report=dict(template.policy_report),
        plan={
            **template.plan,
            "recipe_revision_id": revision.id,
            "recipe_content_sha256": revision.content_digest,
        },
        image_digest=template.image_digest,
        oci_layout_sha256=template.oci_layout_sha256,
        image_bytes=template.image_bytes,
        created_at=now,
        updated_at=now,
    )
    session.add(build)
    session.flush()
    build.plan = {**build.plan, "build_id": build.id}
