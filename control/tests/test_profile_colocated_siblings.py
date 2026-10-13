"""One profile load places several recipes on one Spark, and every one serves.

Production (2026-10-13, the hardware sweep's baseline beside the recipe under
test on one Spark): the load started the first recipe; the second child planned
to stop that run as if it were someone else's, was refused as "an unreviewed
workload" on every retry, and an hour later the load ended cancelled although
nobody had cancelled it.

The real Controller services run over PostgreSQL on a fake clock; fake Sparks
report inventory, answer every order they are given and report the runs they
serve.
"""

from __future__ import annotations

import ast
import copy
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import vonk_control
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.agent_jobs import AgentJobService
from vonk_control.fleet_profile_contract import (
    FleetProfileEffects,
    FleetProfileInput,
    FleetProfileInstallationEffect,
    FleetProfileRunEffect,
)
from vonk_control.fleet_profiles import (
    FleetProfileService,
    build_production_fleet_profile_service,
)
from vonk_control.fleet_profiles.contracts import FleetProfileReviewStale
from vonk_control.fleet_profiles.projection_support import _validate_remaining_effects
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentOperation,
    AgentPresence,
    CatalogDocument,
    CatalogDocumentRevision,
    FleetProfileApplication,
    Job,
    RecipeBuild,
    RecipeRun,
    RunNode,
)
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.recipe_routes import RecipeRouteService
from vonk_control.run_switch_operations import RunSwitchOperationService
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .test_lifecycle_preflight import _finish as complete_preflight
from .test_recipe_operations import ConcurrentPublisher, setup_services, start_evidence
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)

#: The recipe that needs both Sparks and two different ones that run on one.
PAIR, SOLO_A, SOLO_B = "pair", "solo-a", "solo-b"
_ANSWERED = (
    "recipe.install",
    "recipe.start",
    "recipe.stop",
    "recipe.uninstall",
    "runtime.preflight.v1",
)
_ENDED = ("succeeded", "failed", "cancelled", "superseded")
#: Well past the hour after which a waiting load used to end itself.
_PAST_THE_OLD_WINDOW = timedelta(hours=2)


@dataclass
class _Fleet:
    sessions: sessionmaker[Session]
    nodes: tuple[str, ...]
    recipes: dict[str, str]
    now: list[datetime]
    profiles: FleetProfileService
    lifecycle: RecipeOperationService
    worker: RecipeOperationWorker
    serving: dict[str, set[str]] = field(default_factory=dict)
    #: Sparks whose inventory reports stop (they still answer and serve).
    silent: set[str] = field(default_factory=set)

    def assignment(self, recipe: str, nodes: tuple[str, ...]) -> dict[str, object]:
        return {
            "recipe_selector": f"vonk-forge/{self.recipes[recipe]}",
            "spark_ids": list(nodes),
            "desired_state": "running",
            "assignment_name": f"{recipe}-{len(nodes)}",
        }

    def profile(self, name: str, assignments: list[dict[str, object]]) -> str:
        return self.profiles.create(
            FleetProfileInput.model_validate(
                {"name": name, "assignments": assignments}
            ),
            actor="admin",
        ).id

    def load(self, profile_id: str) -> str:
        return self.profiles.apply(
            profile_id, request_key=str(uuid.uuid4()), actor="admin"
        ).id

    def state(self, application_id: str) -> str:
        return self.profiles.application(application_id).state

    def round(self, seconds: float = 5) -> None:
        """One worker pass, then one poll of every Spark that is up."""

        self.now[0] += timedelta(seconds=seconds)
        self.worker.tick()
        for node in self.nodes:
            if node not in self.silent:
                self._report_inventory(node)
            self._answer(node)
        self._report_serving()

    def settle(self, application_id: str, rounds: int = 120) -> str:
        """Rounds until the load ends, following the retries it hands over to."""

        for _ in range(rounds):
            state = self.state(application_id)
            if state == "superseded":
                application_id = self._successor(application_id) or application_id
            elif state in _ENDED:
                return state
            self.round()
        return self.state(application_id)

    def serving_on(self, node: str) -> set[str]:
        """The serving runs a Spark reports, as the Controller sees them."""

        with self.sessions() as session:
            return {
                run.id
                for run in session.scalars(
                    select(RecipeRun)
                    .join(RunNode, RunNode.run_id == RecipeRun.id)
                    .where(
                        RunNode.node_id == node,
                        RecipeRun.state == "running",
                        RecipeRun.route_state == "published",
                    )
                )
                if run.id in self.serving.get(node, set())
            }

    def _successor(self, application_id: str) -> str | None:
        """The load that took over a superseded one (its retry, or a newer load)."""

        with self.sessions() as session:
            ended = session.get(FleetProfileApplication, application_id)
            assert ended is not None
            return session.scalar(
                select(FleetProfileApplication.id)
                .where(
                    FleetProfileApplication.profile_id == ended.profile_id,
                    FleetProfileApplication.created_at >= ended.created_at,
                    FleetProfileApplication.id != application_id,
                )
                .order_by(FleetProfileApplication.created_at.desc())
                .limit(1)
            )

    def _report_inventory(self, node: str) -> None:
        now = self.now[0]
        inventory = InventoryRepository(self.sessions, clock=lambda: now)
        latest = inventory.latest(node, now=now, maximum_age=10**9)
        if latest.observed_at >= now:
            return
        inventory.record(
            InventorySnapshotInput(
                node,
                now,
                latest.disk_total_bytes,
                latest.disk_free_bytes,
                latest.host_memory_total_bytes,
                latest.host_memory_free_bytes,
                latest.gpu_memory_total_bytes,
                latest.gpu_memory_free_bytes,
                latest.gpu_count,
                latest.artifact_store_read_only,
                latest.capabilities,
                memory_pool=latest.memory_pool,
                fabric_address=latest.fabric_address,
                fabric_bandwidth_mbps=latest.fabric_bandwidth_mbps,
            )
        )
        with self.sessions.begin() as session:
            presence = session.get(AgentPresence, node)
            if presence is not None:
                presence.observed_at = now

    def _answer(self, node: str) -> None:
        with self.sessions() as session:
            orders = session.execute(
                select(Job.id, Job.kind, AgentOperation.payload)
                .join(AgentOperation, AgentOperation.parent_job_id == Job.id)
                .where(
                    Job.kind.in_(_ANSWERED),
                    AgentOperation.node_id == node,
                    AgentOperation.state.not_in(_ENDED),
                )
                .order_by(Job.created_at, Job.id, AgentOperation.id)
            ).all()
        for job_id, kind, raw in orders:
            payload = dict(raw or {})
            if kind == "runtime.preflight.v1":
                complete_preflight(
                    self.sessions, SimpleNamespace(pending_job_id=job_id), self.now[0]
                )
                continue
            evidence: dict[str, object] = (
                {"installed_bytes": 120}
                if kind == "recipe.install"
                else start_evidence(payload)
                if kind == "recipe.start"
                else {}
            )
            self.lifecycle.record_node_result(
                job_id, node, succeeded=True, evidence=evidence
            )
            run_id = payload.get("run_id")
            if isinstance(run_id, str) and kind == "recipe.start":
                self.serving.setdefault(node, set()).add(run_id)
            elif isinstance(run_id, str) and kind == "recipe.stop":
                self.serving.setdefault(node, set()).discard(run_id)

    def _report_serving(self) -> None:
        now = self.now[0]
        with self.sessions.begin() as session:
            for node, runs in self.serving.items():
                if not runs:
                    continue
                for member in session.scalars(
                    select(RunNode).where(
                        RunNode.node_id == node, RunNode.run_id.in_(runs)
                    )
                ):
                    run = session.get(RecipeRun, member.run_id)
                    member.updated_at = now
                    if run is None or run.plan.get("observation_schema_version") != 2:
                        continue
                    member.observed_run_generation = run.run_generation
                    member.observation_process_running = True
                    member.observation_observed_at = now
                    member.observation_endpoint_ready = (
                        True if member.role == "entrypoint" else None
                    )


def _fleet(tmp_path, engine) -> _Fleet:
    sessions, template, _queue, _mapping, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=engine
    )
    now = [template._clock()]
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        pair = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert pair is not None
        recipes = {PAIR: pair.slug}
        for recipe in (SOLO_A, SOLO_B):
            recipes[recipe] = _publish_single_spark(
                session, pair, build, f"qwen3-{recipe}", now[0]
            )

    def clock() -> datetime:
        return now[0]

    lifecycle = RecipeOperationService(
        sessions,
        install_admission=template._install_admission,
        run_admission=template._run_admission,
        agent_jobs=template._agent_jobs,
        clock=clock,
    )
    planner = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    profiles = build_production_fleet_profile_service(
        sessions, clock=clock, run_switch_operations=planner
    )
    orders = AgentJobService(
        sessions, clock=clock, result_consumer=lifecycle.consume_agent_result
    )
    worker = RecipeOperationWorker(
        sessions,
        RecipeRouteService(
            sessions,
            publisher=ConcurrentPublisher(),
            management_policy=ManagementAddressPolicy.parse("192.168.1.0/24"),
            clock=clock,
        ),
        clock=clock,
        order_reconcile=orders.reconcile_orders,
        stop_admission_cleanup=lifecycle.reconcile_pending_service_stops,
        build_cleanup=lifecycle.reconcile_cancelled_builds,
        retirement_cleanup=lifecycle.reconcile_retired_operations,
        fleet_profiles=profiles,
        run_switches=planner,
    )
    return _Fleet(sessions, nodes, recipes, now, profiles, lifecycle, worker)


def _publish_single_spark(
    session: Session,
    pair: CatalogDocumentRevision,
    build: RecipeBuild,
    slug: str,
    now: datetime,
) -> str:
    """The pair recipe reduced to its endpoint role on one Spark, with its build."""

    document: dict[str, Any] = copy.deepcopy(pair.document)
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
    canonical = RecipeDefinition.model_validate(document).model_dump(mode="json")
    catalog = CatalogDocument(
        kind="recipe",
        publisher=pair.publisher,
        slug=slug,
        title=canonical["metadata"]["title"],
        created_by="admin",
        created_at=now,
        updated_at=now,
    )
    session.add(catalog)
    session.flush()
    revision = CatalogDocumentRevision(
        id=str(uuid.uuid4()),
        document_id=catalog.id,
        kind="recipe",
        publisher=canonical["identity"]["publisher"],
        slug=slug,
        revision_number=1,
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
    receipt = RecipeBuild(
        recipe_revision_id=revision.id,
        builder_node_id=build.builder_node_id,
        source_bundle_sha256=build.source_bundle_sha256,
        build_input_sha256=build.build_input_sha256,
        state="succeeded",
        policy_report=dict(build.policy_report),
        plan={
            **build.plan,
            "recipe_revision_id": revision.id,
            "recipe_content_sha256": revision.content_digest,
        },
        image_digest=build.image_digest,
        oci_layout_sha256=build.oci_layout_sha256,
        image_bytes=build.image_bytes,
        created_at=now,
        updated_at=now,
    )
    session.add(receipt)
    session.flush()
    receipt.plan = {**receipt.plan, "build_id": receipt.id}
    return slug


def _fresh_load_serves(fleet: _Fleet, profile_id: str, ended: str) -> None:
    """Whatever came before, a fresh load is admitted and ends serving."""

    fresh = fleet.load(profile_id)
    assert fresh != ended
    assert fleet.settle(fresh) == "succeeded"


# Each test drives the whole worker loop for a few hundred rounds over
# PostgreSQL, like the production worker does.
@pytest.mark.slow(30)
def test_one_load_places_two_recipes_on_one_spark_and_both_serve(
    tmp_path, postgres_engine
) -> None:
    fleet = _fleet(tmp_path, postgres_engine)
    spark = fleet.nodes[:1]
    profile_id = fleet.profile(
        "Two recipes on one Spark",
        [fleet.assignment(SOLO_A, spark), fleet.assignment(SOLO_B, spark)],
    )
    load = fleet.load(profile_id)

    assert fleet.settle(load) == "succeeded"
    assert len(fleet.serving_on(spark[0])) == 2
    _fresh_load_serves(fleet, profile_id, load)
    assert len(fleet.serving_on(spark[0])) == 2


@pytest.mark.slow(30)
def test_a_pair_on_both_sparks_and_a_recipe_beside_it_all_serve(
    tmp_path, postgres_engine
) -> None:
    fleet = _fleet(tmp_path, postgres_engine)
    first, second = fleet.nodes
    profile_id = fleet.profile(
        "Pair with a recipe beside it",
        [fleet.assignment(PAIR, fleet.nodes), fleet.assignment(SOLO_B, (first,))],
    )
    load = fleet.load(profile_id)

    assert fleet.settle(load) == "succeeded"
    (pair_run,) = fleet.serving_on(second)
    assert fleet.serving_on(first) - {pair_run}
    _fresh_load_serves(fleet, profile_id, load)


@pytest.mark.slow(30)
def test_a_replacement_load_of_the_same_assignments_ends_serving(
    tmp_path, postgres_engine
) -> None:
    fleet = _fleet(tmp_path, postgres_engine)
    spark = fleet.nodes[:1]
    profile_id = fleet.profile(
        "Two recipes on one Spark",
        [fleet.assignment(SOLO_A, spark), fleet.assignment(SOLO_B, spark)],
    )
    first = fleet.load(profile_id)
    while not fleet.serving_on(spark[0]):
        fleet.round()

    # Loaded again while the first load has started only one of its runs.
    replacement = fleet.load(profile_id)

    assert fleet.settle(replacement) == "succeeded"
    assert fleet.state(first) != "cancelled"
    assert len(fleet.serving_on(spark[0])) == 2


@pytest.mark.slow(30)
def test_a_recipe_under_test_joins_a_serving_baseline_on_its_spark(
    tmp_path, postgres_engine
) -> None:
    """The hardware sweep: a baseline serves, then a profile of the baseline
    and the recipe under test on the same Spark is loaded."""

    fleet = _fleet(tmp_path, postgres_engine)
    spark = fleet.nodes[:1]
    baseline = fleet.load(fleet.profile("Baseline", [fleet.assignment(SOLO_A, spark)]))
    assert fleet.settle(baseline) == "succeeded"
    (baseline_run,) = fleet.serving_on(spark[0])
    sweep_profile = fleet.profile(
        "Baseline and the recipe under test",
        [fleet.assignment(SOLO_A, spark), fleet.assignment(SOLO_B, spark)],
    )
    sweep = fleet.load(sweep_profile)

    assert fleet.settle(sweep) == "succeeded"
    serving = fleet.serving_on(spark[0])
    assert len(serving) == 2 and baseline_run in serving
    _fresh_load_serves(fleet, sweep_profile, sweep)


@pytest.mark.slow(40)
def test_a_load_whose_second_child_waits_is_never_ended_without_a_request(
    tmp_path, postgres_engine
) -> None:
    fleet = _fleet(tmp_path, postgres_engine)
    spark = fleet.nodes[:1]
    profile_id = fleet.profile(
        "Two recipes on one Spark",
        [fleet.assignment(SOLO_A, spark), fleet.assignment(SOLO_B, spark)],
    )
    load = fleet.load(profile_id)
    while not fleet.serving_on(spark[0]):
        fleet.round()
    fleet.silent.add(spark[0])

    # The second child cannot be planned without a current inventory; the fake
    # clock runs well past the hour after which the load used to end itself.
    waited = timedelta()
    while waited < _PAST_THE_OLD_WINDOW:
        fleet.round(120)
        waited += timedelta(seconds=120)
        waiting = fleet.profiles.application(load)
        assert waiting.state == "running", waiting.status_reason
        assert waiting.progress.cancellation is None
    assert "Waiting" in (waiting.status_reason or "")

    fleet.silent.clear()
    assert fleet.settle(load) == "succeeded"
    assert len(fleet.serving_on(spark[0])) == 2
    _fresh_load_serves(fleet, profile_id, load)


def test_a_retry_may_replace_its_own_run_but_never_stop_another_workload() -> None:
    """Replanning recognises a run on the profile's own installation as its own."""

    node = ["spk_" + "1" * 32]
    own, foreign = str(uuid.uuid4()), str(uuid.uuid4())
    reviewed = FleetProfileEffects(runs=[], installations=[], superseded=[])

    def remaining(installation_id: str) -> FleetProfileEffects:
        return FleetProfileEffects(
            runs=[
                FleetProfileRunEffect(
                    run_id=str(uuid.uuid4()),
                    installation_id=installation_id,
                    alias="qwen",
                    node_ids=node,
                    action="stop",
                )
            ],
            installations=[
                FleetProfileInstallationEffect(
                    installation_id=own, node_ids=node, action="keep"
                )
            ],
            superseded=[],
        )

    _validate_remaining_effects(reviewed, remaining(own))
    with pytest.raises(FleetProfileReviewStale):
        _validate_remaining_effects(reviewed, remaining(foreign))


def test_only_a_cancel_request_cancels_an_issued_load() -> None:
    """Guard: no timer, retry or replan fabricates a cancel on a load's behalf.

    Covers the cancellation that stops a load's issued effects. Ending a load
    that is still waiting for admission is a separate path.
    """

    source = Path(vonk_control.__file__).parent / "fleet_profiles"
    request = source / "cancellation.py"
    fabricated = sorted(
        f"{path.relative_to(source)}:{node.lineno}"
        for path in source.rglob("*.py")
        if path != request
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
        in {"FleetProfileApplicationCancellationIntent", "request_cancel"}
    )
    assert not fabricated, f"only a cancel request cancels a load: {fabricated}"
