"""Unused storage collection: scope budget guards."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from vonk_agent_protocol.capability_reply import ControllerCapability
from vonk_agent_protocol.contracts import AgentOperation as OperationKind
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState, WaitReason
from vonk_agent_protocol.state_machines import RunState
from vonk_control.capabilities import CapabilityRegistry
from vonk_control.catalog.revision_collection import GRACE
from vonk_control.distributed_recovery.common import (
    _settle_unrecoverable,
    release_inactive_run_claims_in_session,
)
from vonk_control.fleet_profile_contract.effects import (
    FleetProfileInstallationEffect,
)
from vonk_control.fleet_profile_contract.review import FleetProfilePreview
from vonk_control.jobs.documents import RecipeInstallParent
from vonk_control.model_cache.service import ModelCacheService
from vonk_control.model_primitives import Base
from vonk_control.models.cache import ArtifactLifecycleGate
from vonk_control.models.catalog import (
    CatalogDocumentRevision,
)
from vonk_control.models.execution import (
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.models.fleet import (
    AgentNode,
    FleetProfileApplication,
    FleetProfileSelection,
)
from vonk_control.models.jobs import Job
from vonk_control.persisted_waits import read_wait
from vonk_control.recipe_operations.errors import RecipeRetryLater
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage
from vonk_control.storage_demands import (
    NAS_MODELS,
    StorageDemands,
    spark_scope,
)
from vonk_control.unused_storage_collection import pressure as unused_storage_pressure
from vonk_control.unused_storage_collection.common import ACTOR, spark_eviction_capacity
from vonk_control.unused_storage_collection.evidence import _Evidence
from vonk_control.unused_storage_collection.service import UnusedStorageCollector

from .model_cache_support import _artifact, _download
from .recipe_operations_support import started_recipe
from .test_catalog_revision_collection import NODE, NOW, Catalog
from .unused_storage_collection_support import (
    _REAL_CLOCK,
    AN_IMAGE,
    GIB,
    TOTAL,
    FakeLifecycle,
    _agents_succeed,
    _application,
    _collector,
    _kept,
    _models,
    _profile,
    _real_collector,
    _real_lifecycle,
    _receipt,
    _report_disk,
    _required,
    _scope,
    _size,
)


def _queued_load(world: Catalog, installation_id: str) -> None:
    """A load that has been accepted and names this exact installation."""

    with world.sessions.begin() as session:
        session.add(
            Job(
                request_id=str(uuid.uuid4()),
                kind=OperationKind.RECIPE_INSTALL.value,
                state=LifecycleState.QUEUED,
                actor="test",
                authority_revision="r",
                targets=[NODE],
                payload_digest="0" * 64,
                payload=RecipeInstallParent(
                    schema_version=1,
                    owner_kind="installation",
                    owner_id=installation_id,
                    plan_digest="f" * 64,
                ).model_dump(mode="json"),
                current_attempt=0,
                created_at=NOW,
                updated_at=NOW,
            )
        )


@pytest.mark.parametrize(
    "ended",
    [
        LifecycleState.SUCCEEDED,
        LifecycleState.FAILED,
        LifecycleState.CANCELLED,
        LifecycleState.SUPERSEDED,
        None,
    ],
)
def test_selected_application_releases_historical_installation_hold(
    postgres_engine: Engine,
    ended: LifecycleState | None,
) -> None:
    """A selected snapshot must not retain obsolete effects after its owner ends."""

    Base.metadata.create_all(postgres_engine)
    world = Catalog(postgres_engine)
    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    installation, _ = world.workload(old, run=RunState.STOPPED)
    application_id = _application(
        world,
        {},
        state=LifecycleState.RUNNING,
        selected=True,
        canonical=True,
    )
    with world.sessions.begin() as session:
        application = _required(session.get(FleetProfileApplication, application_id))
        plan = FleetProfilePreview.model_validate_json(json.dumps(application.plan))
        plan.effects.installations = [
            FleetProfileInstallationEffect(
                installation_id=installation,
                node_ids=[NODE],
                action="keep",
            )
        ]
        application.plan = plan.model_dump(mode="json")
    with world.sessions() as session:
        assert installation in _Evidence.read(session, NOW).operations
    # Simulate completion/cancellation/supersession or loss between reconciles.
    with world.sessions.begin() as session:
        application = _required(session.get(FleetProfileApplication, application_id))
        if ended is None:
            session.delete(_required(session.get(FleetProfileSelection, 1)))
            session.flush()
            session.delete(application)
        else:
            application.state = ended
            application.updated_at = NOW
    with world.sessions() as session:
        assert installation not in _Evidence.read(session, NOW).operations


def test_a_load_accepted_before_the_removal_is_queued_stops_the_removal(
    world: Catalog,
) -> None:
    """Catches a removal that races a load: the guard re-proves under lock."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.workload(old, run="stopped")
    lifecycle = FakeLifecycle(
        world.sessions,
        between=lambda installation_id: _queued_load(world, installation_id),
    )

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "live operation") == 1


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_profile_saved_before_the_removal_is_queued_stops_the_removal(
    world: Catalog,
) -> None:
    """The guard re-reads the profiles, not only the sweep's first snapshot."""

    head = world.revision("glm", 1, head="active")
    world.workload(head, run="stopped")

    def load_profile(_id: str) -> None:
        _profile(world, "vonk-forge/glm")
        _application(world, {}, state="succeeded", selected=True)

    lifecycle = FakeLifecycle(world.sessions, between=load_profile)

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "profile") == 1


def test_a_failed_installation_nothing_uses_goes_like_an_installed_one(
    world: Catalog,
) -> None:
    """Catches a failed install holding its disk for ever as "not installed"."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    failed, _ = world.workload(old, installation="failed", run="stopped")
    _size(world, failed, 80 * GIB)
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == [failed]
    assert _kept(result, "not installed") == 0


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_failed_installation_a_retry_names_is_kept(world: Catalog) -> None:
    """Catches removing a failed install while its retry is queued."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    failed, _ = world.workload(old, installation="failed", run="stopped")
    _size(world, failed, 80 * GIB)
    world.job({"plan": {"installation_id": failed}}, state="observing", updated=NOW)
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "live operation") == 1


def test_installations_that_may_hold_partial_files_are_left_to_reconcile(
    world: Catalog,
) -> None:
    """Partial installs belong to the exact reconcile path."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.workload(old, installation="partial", run="stopped")
    lifecycle = FakeLifecycle(world.sessions)

    _collector(world, lifecycle).collect()

    assert lifecycle.removed == []


def test_one_refused_installation_does_not_stop_the_others(world: Catalog) -> None:
    """Catches a sweep that stops at the first refusal."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    first, _ = world.workload(old, run="stopped")
    second, _ = world.workload(old, run="stopped")
    lifecycle = FakeLifecycle(world.sessions, refuse=frozenset({first}))

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == [second]
    assert _kept(result, "refused") == 1


def test_unknown_removal_is_deferred_and_retried_without_blocking_other_items(
    world, monkeypatch
):
    """Catches treating temporary ownership uncertainty as a refusal or hot loop."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    first, _ = world.workload(old, run="stopped")
    second, _ = world.workload(old, run="stopped")
    lifecycle = FakeLifecycle(world.sessions, apply=True)
    collector = _collector(world, lifecycle)
    actual = lifecycle.uninstall
    calls = []

    def uninstall(installation_id, **kwargs):
        calls.append(installation_id)
        if installation_id == first and calls.count(first) == 1:
            raise RecipeRetryLater(
                "unknown ownership", reason=WaitReason.OBSERVATION_UNAVAILABLE
            )
        return actual(installation_id, **kwargs)

    monkeypatch.setattr(lifecycle, "uninstall", uninstall)
    result = collector.collect()
    assert lifecycle.removed == [second]
    assert _kept(result, "deferred") == 1

    with world.sessions() as session:
        retained = session.get(RecipeInstallation, first)
        assert retained is not None
        assert read_wait(retained) is not None
    collector = _collector(world, lifecycle, now=world.now)
    _report_disk(world.sessions, GIB + 2, at=world.now)
    collector.collect()
    assert calls.count(first) == 1, "unknown outcome retried without backoff"
    world.now += collector._scan_interval
    collector.collect()
    assert calls.count(first) == 2
    assert first in lifecycle.removed
    with world.sessions() as session:
        settled = session.get(RecipeInstallation, first)
        assert settled is not None
        assert read_wait(settled) is None


def test_a_sweep_that_runs_out_of_budget_continues_on_the_next_pass(
    world: Catalog, frozen_pass_budgets
) -> None:
    """Catches waiting a whole interval to finish a sweep the budget cut short."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    for _ in range(3):
        world.workload(old, run="stopped")
    lifecycle = FakeLifecycle(
        world.sessions,
        between=lambda _id: frozen_pass_budgets.advance(_PASS_BUDGET + 1),
    )
    collector = _collector(world, lifecycle, budget_seconds=_PASS_BUDGET)

    # Each scope's first attempt always runs and spends the pass's attempt
    # budget, so the third installation waits for the next pass.
    assert collector.tick() is True
    assert len(lifecycle.removed) == 2

    assert collector._due_at == collector._clock()  # continues at once
    assert collector.tick() is True
    assert len(lifecycle.removed) == 3


def test_evidence_slower_than_the_budget_still_advances_the_pass(
    world: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches fixed setup spending the pass budget, so no pass ever progresses."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.workload(old, run="stopped")
    lifecycle = FakeLifecycle(world.sessions)
    collector = _collector(world, lifecycle, budget_seconds=1.0)
    elapsed = [0.0]
    read = _Evidence.read

    def slow_read(session: Session, now: datetime) -> _Evidence:
        elapsed[0] += 2.0
        return read(session, now)

    clock = SimpleNamespace(monotonic=lambda: elapsed[0])
    monkeypatch.setattr(unused_storage_pressure, "time", clock)
    monkeypatch.setattr(_Evidence, "read", slow_read)

    assert collector.tick() is True
    assert len(lifecycle.removed) == 1


_INSTALLATION_SLOW_STEPS = (
    "evidence read",
    "spark inventory",
    "expiry image inventory",
)


_NAS_SLOW_STEPS = ("model inventory", "image inventory")


_PASS_BUDGET = 5.0  # passed to every collector below, never the default


@pytest.mark.parametrize("step", _NAS_SLOW_STEPS)
def test_a_slow_nas_inventory_step_never_spends_the_pass_budget(
    world: Catalog, cached, frozen_pass_budgets, step: str
) -> None:
    """Catches any NAS inventory step slower than the budget leaving the pass
    with nothing attempted, so the next pass repeats it forever."""

    service, _ = cached
    demands = StorageDemands(lambda: world.now)
    collector = _models(
        world,
        service,
        free=200 * GIB,
        demands=demands,
        budget_seconds=_PASS_BUDGET,
    )
    demands.request(
        NAS_MODELS,
        200 * GIB + 3,
        source="model-download",
        reason="insufficient-reserved-storage",
    )
    if step == "model inventory":
        measure = service.unused_set_bytes

        def slow_bytes(digest: str) -> int | None:
            frozen_pass_budgets.advance(_PASS_BUDGET + 1)
            return measure(digest)

        service.unused_set_bytes = slow_bytes  # type: ignore[method-assign]
    else:
        images = collector._image_items

        def slow_images(*args, **kwargs):
            frozen_pass_budgets.advance(_PASS_BUDGET + 1)
            return images(*args, **kwargs)

        collector._image_items = slow_images  # type: ignore[method-assign]

    result = collector.collect()

    assert result.models == 1, result.kept
    assert _kept(result, "sweep budget") == 0
    # The refused download is served, not told nothing could be freed.
    assert _scope(result, "nas")["outcome"] != "insufficient_after_eviction"


@pytest.mark.parametrize("step", _INSTALLATION_SLOW_STEPS)
def test_a_slow_inventory_step_still_attempts_an_installation_removal(
    world: Catalog, frozen_pass_budgets, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """Catches the pass budget starting before an inventory step: each slow
    step advances the pass clock past the budget and the pass must still
    attempt at least one item."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.workload(old, run="stopped")
    lifecycle = FakeLifecycle(world.sessions)
    # Expiry alone (no pressure) must not be starved by its own inventory.
    free = TOTAL // 2 if step == "expiry image inventory" else GIB
    collector = _collector(world, lifecycle, free=free, budget_seconds=_PASS_BUDGET)

    def slowed(original):
        def wrapper(*args, **kwargs):
            frozen_pass_budgets.advance(_PASS_BUDGET + 1)
            return original(*args, **kwargs)

        return wrapper

    if step == "evidence read":
        monkeypatch.setattr(_Evidence, "read", slowed(_Evidence.read))
    elif step == "spark inventory":
        original = UnusedStorageCollector._spark_items
        monkeypatch.setattr(
            UnusedStorageCollector, "_spark_items", staticmethod(slowed(original))
        )
    else:
        collector._image_items = slowed(collector._image_items)  # type: ignore[method-assign]

    result = collector.collect()

    assert lifecycle.removed, result.kept
    assert _kept(result, "sweep budget") == 0


def test_many_scopes_share_one_attempt_budget_and_each_attempts_its_first_item(
    world: Catalog, tmp_path: Path, frozen_pass_budgets
) -> None:
    """Catches per-scope budgets (a pass with N scopes outgrowing the worker's
    watchdog) and inventory time spent from the attempt budget: slow
    inventories everywhere, NAS + two pressured Sparks + expiry."""

    # Not the installations' image digest, which would make the model live.
    service = ModelCacheService(
        world.sessions, tmp_path / "nas-cache", free_fraction=0, fixture_sources=True
    )
    model = "c" * 64
    downloaded = _download(
        service,
        [_artifact(tmp_path, b"weights", model_content_sha256=model)],
        model_content_sha256=model,
        request_key=str(uuid.uuid4()),
    )
    assert downloaded.state == "succeeded"
    spark_a, spark_b, spark_c = NODE, "spk_" + "2" * 32, "spk_" + "3" * 32
    other_model = {"models": [{"model": {"content_sha256": "b" * 64}}]}
    old = world.revision("glm", 1, document=other_model)
    world.revision("glm", 2, head="active", document=other_model)
    on: dict[str, list[str]] = {spark_a: [], spark_b: [], spark_c: []}
    for node, count in ((spark_a, 2), (spark_b, 2), (spark_c, 1)):
        for _ in range(count):
            installation, _run = world.workload(old, run="stopped")
            on[node].append(installation)
            _size(world, installation, 80 * GIB, model="b" * 64)
            if node != NODE:
                with world.sessions.begin() as session:
                    if session.get(AgentNode, node) is None:
                        session.add(
                            AgentNode(
                                node_id=node, state="active", workload_intent_ordinal=1
                            )
                        )
                        session.flush()
                    row = session.scalar(
                        select(InstallationNode).where(
                            InstallationNode.installation_id == installation
                        )
                    )
                    assert row is not None
                    row.node_id = node
    attempt_cost = 10.0
    lifecycle = FakeLifecycle(
        world.sessions,
        between=lambda _id: frozen_pass_budgets.advance(attempt_cost),
    )
    collector = _collector(
        world,
        lifecycle,
        now=datetime.now(UTC) + GRACE + timedelta(hours=1),
        model_cache=service,
        model_cache_root=service._root,
        budget_seconds=25.0,
    )
    _report_disk(world.sessions, GIB, at=world.now, node=spark_b)
    _report_disk(world.sessions, 900 * GIB, at=world.now, node=spark_c)

    def slow(original):
        def wrapper(*args, **kwargs):
            frozen_pass_budgets.advance(26.0)
            return original(*args, **kwargs)

        return wrapper

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(_Evidence, "read", slow(_Evidence.read))
        monkey.setattr(service, "unused_set_bytes", slow(service.unused_set_bytes))
        monkey.setattr(
            UnusedStorageCollector,
            "_spark_items",
            staticmethod(slow(UnusedStorageCollector._spark_items)),
        )
        collector._image_items = slow(collector._image_items)  # type: ignore[method-assign]
        before = frozen_pass_budgets.seconds
        result = collector.collect()
    finally:
        monkey.undo()

    service.close()
    removed = set(lifecycle.removed)
    assert removed & set(on[spark_a]), result.kept
    assert removed & set(on[spark_b]), result.kept
    # NAS, two Spark scopes and expiry: each scope's first item, no more.
    assert len(removed) == 3, result.kept
    assert result.models == 1, result.kept
    # Past the shared budget only each scope's first attempt runs.
    assert _kept(result, "sweep budget") >= 1
    # Attempts are counted; the four slow inventories are not.
    assert collector._removal_seconds >= attempt_cost * len(removed)
    assert frozen_pass_budgets.seconds - before > 25.0 * 4  # inventories were slow
    assert collector._due_at == collector._clock()


def test_one_sweep_per_interval(world: Catalog) -> None:
    """Catches a sweep on every worker pass."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.workload(old, run="stopped")
    lifecycle = FakeLifecycle(world.sessions)
    collector = _collector(world, lifecycle)

    assert collector.tick() is True
    assert collector.tick() is False
    assert len(lifecycle.removed) == 1


def _ordinals(sessions: sessionmaker[Session]) -> dict[str, int]:
    with sessions() as session:
        return {
            node.node_id: node.workload_intent_ordinal
            for node in session.scalars(select(AgentNode))
        }


def test_unused_installation_is_uninstalled_without_taking_a_new_intent(
    tmp_path: Path,
) -> None:
    """Catches an unattended removal that supersedes older orders or the recovery
    of a workload running on the same Sparks (a newer intent ends recovery)."""

    sessions, service, _installation_id, nodes = _real_lifecycle(tmp_path)
    before = _ordinals(sessions)
    collector = _real_collector(sessions, service, nodes)

    result = collector.collect()

    assert result.installations == 1, result.kept
    with sessions() as session:
        job = session.scalar(select(Job).where(Job.kind == "recipe.uninstall"))
        assert job is not None
        assert job.actor == ACTOR
        assert job.payload["workload_intent_ordinal"] == before[nodes[0]]
    assert _ordinals(sessions) == before


@pytest.mark.parametrize("below_line", [False, True])
@pytest.mark.parametrize("sparks", [1, 2])
def test_a_failed_run_on_an_idle_spark_is_stopped_then_its_disk_freed_for_a_load(
    tmp_path: Path, sparks: int, below_line: bool
) -> None:
    """Fault: a run settled failed (never stopped) pins its installation on a
    Spark where nothing else runs, and a load needs that disk. Catches the load
    refused with "its run was never stopped": the review plans the eviction,
    the collector queues the run's exact Stop (taking no new intent), the load
    keeps waiting while it lands, the run is recorded stopped, the next pass
    uninstalls, and the load fits.

    Below the low-space line (production spark-3542: 73 GB free of 4 TB, a
    178 GB installation, a load short by 29 GB) catches the pass that measured
    the load against the line instead of its own gap: it judged everything
    removable "insufficient" and removed nothing while relief told the load
    "evicting", so the load waited out its budget."""

    sessions, service, installation_id, nodes = _real_lifecycle(tmp_path, nodes=sparks)
    run_id = started_recipe(
        sessions, service, installation_id, nodes, request_id=str(uuid.uuid4())
    ).owner_id
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        ranks = session.scalars(select(RunNode).where(RunNode.run_id == run_id)).all()
        for rank in ranks:
            rank.state = RunState.FAILED  # every rank's process died
        _settle_unrecoverable(run, ranks, "fault: unrecoverable", _REAL_CLOCK)
        release_inactive_run_claims_in_session(session, _REAL_CLOCK)
        held = session.scalar(
            select(InstallationNode.installed_bytes).where(
                InstallationNode.installation_id == installation_id
            )
        )
    assert held is not None
    assert held > 0
    before = _ordinals(sessions)
    collector = _real_collector(sessions, service, nodes)
    collector._demands = StorageDemands(lambda: _REAL_CLOCK)
    # Above the low line only the load asks for space; below it the line's gap
    # is larger than everything removable, the load's own gap is not.
    free = TOTAL // 50 if below_line else TOTAL // 2
    assert (held < TOTAL // 10 - free) is below_line
    _report_disk(sessions, free, at=_REAL_CLOCK, node=nodes[0])
    # Above the line any other Spark of the run is short on its own, so one
    # sweep relieves two Sparks: neither may remove more while the shared Stop
    # lands. Below it the other Spark has room (production's spark-2297), so
    # only the load's Spark can free the installation.
    for other in nodes[1:] if below_line else ():
        _report_disk(sessions, TOTAL // 2, at=_REAL_CLOCK, node=other)

    def relief():
        return collector.relief_for_spark(
            nodes[0], free + held, source="profile-load", subject="p", reason="x"
        )

    with sessions() as session:
        assert spark_eviction_capacity(session, nodes[0], _REAL_CLOCK).freeable >= held
    assert _required(relief()).code == "storage.evicting"

    first = collector.collect()

    with sessions() as session:
        stop = session.scalar(select(Job).where(Job.kind == "recipe.stop"))
        assert stop is not None and stop.actor == ACTOR, first.scopes
        assert session.scalar(select(Job).where(Job.kind == "recipe.uninstall")) is None
    assert _ordinals(sessions) == before
    # The pass measures the load's gap, the one relief told the load.
    assert _scope(first, spark_scope(nodes[0]))["needed_bytes"] == held
    # While the Stop lands the load waits; it is not refused.
    assert _required(relief()).code == "storage.evicting"

    _agents_succeed(sessions, service, stop.id)
    with sessions() as session:
        assert _required(session.get(RecipeRun, run_id)).state == "stopped"
    collector._due_at = None
    assert collector.collect().installations == 1
    with sessions() as session:
        uninstall = session.scalar(select(Job).where(Job.kind == "recipe.uninstall"))
        assert uninstall is not None
    _agents_succeed(sessions, service, uninstall.id)
    with sessions() as session:
        removed = _required(session.get(RecipeInstallation, installation_id))
        assert removed.state == "uninstalled"
    _report_disk(
        sessions, free + held, at=_REAL_CLOCK + timedelta(seconds=1), node=nodes[0]
    )
    assert relief() is None  # the load fits now


def test_removal_retries_when_the_target_sparks_rejoin_a_shared_intent(
    tmp_path: Path,
) -> None:
    """With no shared intent there is none to join; taking one would supersede."""

    sessions, service, _installation_id, nodes = _real_lifecycle(tmp_path, nodes=2)
    with sessions.begin() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        node.workload_intent_ordinal += 5
    before = _ordinals(sessions)
    collector = _real_collector(sessions, service, nodes)
    clock = [_REAL_CLOCK]
    collector._clock = lambda: clock[0]

    result = collector.collect()

    assert result.installations == 0
    assert sum(result.kept.values()) == 1
    with sessions() as session:
        assert session.scalar(select(Job).where(Job.kind == "recipe.uninstall")) is None
    assert _ordinals(sessions) == before

    # A later authorized intent converges the ranks. Cleanup can now join it,
    # but must neither retry before its scan deadline nor take another intent.
    with sessions.begin() as session:
        for node in session.scalars(select(AgentNode)):
            node.workload_intent_ordinal = max(before.values())
    converged = _ordinals(sessions)
    assert collector.collect().installations == 0
    clock[0] += collector._scan_interval
    for node in nodes:
        _report_disk(sessions, GIB, at=clock[0], node=node)
    recovered = collector.collect()
    assert recovered.installations == 1, recovered.kept
    assert _ordinals(sessions) == converged


def test_removal_a_guard_refuses_queues_nothing(tmp_path: Path) -> None:
    """The guard runs with the Sparks locked and before the job exists."""

    sessions, service, installation_id, nodes = _real_lifecycle(tmp_path)
    before = _ordinals(sessions)

    def refuse(_session: Session) -> None:
        raise RuntimeError("a load arrived")

    observed_error = None
    try:
        service.uninstall(
            installation_id,
            actor=ACTOR,
            request_id=str(uuid.uuid4()),
            unattended_guard=refuse,
        )

    except Exception as error:  # noqa: BLE001 - the effect, not taxonomy, matters
        observed_error = error
    assert observed_error is not None

    with sessions() as session:
        assert session.scalar(select(Job).where(Job.kind == "recipe.uninstall")) is None
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None
        assert installation.state == "installed"
        assert session.scalar(select(func.count()).select_from(RecipeRun)) == 0
        assert session.scalar(
            select(func.count()).select_from(InstallationNode)
        ) == len(nodes)
    assert _ordinals(sessions) == before
    # The refused attempt leaves no queue head or node ownership behind.
    fresh_key = str(uuid.uuid4())
    service.uninstall(
        installation_id,
        actor=ACTOR,
        request_id=fresh_key,
    )
    with sessions() as session:
        assert (
            session.scalar(select(Job.id).where(Job.request_id == fresh_key))
            is not None
        )


def test_the_guard_runs_while_the_target_spark_rows_are_locked(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    """Catches a re-check that runs before the Sparks are locked.

    A load accepts its job with the target Spark rows locked ``FOR UPDATE``.
    While the removal's guard decides, a load must therefore not be able to take
    them: it is either already visible to the guard or waits for the removal.
    """

    sessions, service, installation_id, nodes = _real_lifecycle(
        tmp_path, engine=postgres_engine
    )
    before = _ordinals(sessions)
    deciding, released = threading.Event(), threading.Event()
    outcome: dict[str, object] = {}

    def guard(_session: Session) -> None:
        deciding.set()
        assert released.wait(30)

    def remove() -> None:
        try:
            outcome["view"] = service.uninstall(
                installation_id,
                actor=ACTOR,
                request_id=str(uuid.uuid4()),
                unattended_guard=guard,
            )
        except BaseException as error:  # noqa: BLE001 - reported by the test
            outcome["error"] = error

    worker = threading.Thread(target=remove)
    worker.start()
    try:
        assert deciding.wait(30), outcome
        with sessions() as load:
            try:
                load.execute(
                    select(AgentNode.node_id)
                    .where(AgentNode.node_id.in_(nodes))
                    .with_for_update(nowait=True)
                ).all()
            except Exception:  # noqa: BLE001 - inspect actual concurrent effect
                load.rollback()
            else:
                pytest.fail("concurrent load bypassed the held node lock")
    finally:
        released.set()
        worker.join(30)

    assert "error" not in outcome, outcome
    with sessions() as session:
        job = session.scalar(select(Job).where(Job.kind == "recipe.uninstall"))
        assert job is not None
        assert job.payload["workload_intent_ordinal"] == before[nodes[0]]
    assert _ordinals(sessions) == before


def test_a_failed_run_is_stopped_while_its_recipe_revision_row_is_held(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    """Fault (production b434bf85, 16:59Z): another transaction held the
    recipe revision row, and the Stop of the failed run, which locked that row
    without reading it, ended in a lock timeout pass after pass. Catches a Stop
    that waits on the revision row: it is queued while the row stays held."""

    sessions, service, installation_id, nodes = _real_lifecycle(
        tmp_path, engine=postgres_engine
    )
    run_id = started_recipe(
        sessions, service, installation_id, nodes, request_id=str(uuid.uuid4())
    ).owner_id
    with sessions.begin() as session:
        run = _required(session.get(RecipeRun, run_id))
        ranks = session.scalars(select(RunNode).where(RunNode.run_id == run_id)).all()
        for rank in ranks:
            rank.state = RunState.FAILED  # every rank's process died
        _settle_unrecoverable(run, ranks, "fault: unrecoverable", _REAL_CLOCK)
        release_inactive_run_claims_in_session(session, _REAL_CLOCK)
        revision_id = _required(
            session.get(RecipeInstallation, installation_id)
        ).recipe_revision_id
    collector = _real_collector(sessions, service, nodes)
    outcome: dict[str, object] = {}

    def sweep() -> None:
        try:
            outcome["swept"] = collector.collect()
        except BaseException as error:  # noqa: BLE001 - reported by the test
            outcome["error"] = error

    with sessions() as holder:
        holder.execute(
            select(CatalogDocumentRevision.id)
            .where(CatalogDocumentRevision.id == revision_id)
            .with_for_update()
        ).all()
        worker = threading.Thread(target=sweep)
        worker.start()
        worker.join(15)
        waited = worker.is_alive()
        holder.rollback()
    worker.join(60)

    assert not waited, "the Stop waited on the held revision row"
    assert "error" not in outcome, outcome
    with sessions() as session:
        stop = session.scalar(select(Job).where(Job.kind == "recipe.stop"))
        assert stop is not None and stop.actor == ACTOR, outcome


def test_the_sweep_reads_its_evidence_on_postgres(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    """The sweep's own queries (unions, JSON text search, ordering) run on the
    production database, not only on SQLite."""

    Base.metadata.create_all(postgres_engine)
    world = Catalog(postgres_engine)
    old = world.revision("glm", 1)
    head = world.revision("glm", 2, head="active")
    stale, _ = world.workload(old, run="stopped")
    pointed, _ = world.workload(head, run="stopped")
    _profile(world, "vonk-forge/glm")
    lifecycle = FakeLifecycle(world.sessions)

    _collector(world, lifecycle).collect()

    # What no saved profile points to goes first; the pointed installation (not
    # of the loaded profile) goes after it when space is short.
    assert lifecycle.removed == [stale, pointed]


def test_receipt_removal_runs_on_postgres(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    """The gate lock, publication lock and reference scan on the real database."""

    Base.metadata.create_all(postgres_engine)
    world = Catalog(postgres_engine)
    path = _receipt(tmp_path, AN_IMAGE)

    result = _collector(world, image_cache_root=tmp_path).collect()

    assert not path.exists()
    assert result.images == 1


def test_receipt_no_recipe_needs_is_removed_when_disk_is_short_and_recent_ones_last(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches receipts living forever once nothing names their image, and
    removing a just-used one before an idle one."""

    path = _receipt(tmp_path, AN_IMAGE)
    recent = _receipt(tmp_path, "e" * 64, age=timedelta(hours=2))
    blobs = SimpleNamespace(calls=0)
    blobs.collect = lambda: setattr(blobs, "calls", blobs.calls + 1)  # type: ignore[attr-defined]
    exact = {"reserve_floor_bytes": 0, "reserve_fraction": 0.0}

    # One byte short of the 100 GiB low line: the idle receipt covers it.
    result = _collector(
        world,
        image_cache_root=tmp_path,
        image_blobs=blobs,
        free=100 * GIB - 1,
        **exact,
    ).collect()

    assert not path.exists()
    assert recent.exists()
    assert result.images == 1
    # The image's layers are reclaimed now, not at the next hourly pass.
    assert blobs.calls == 1
    with world.sessions() as session:
        gate = session.scalar(select(ArtifactLifecycleGate))
        assert gate is None or gate.removal_owner_id is None

    # Short by more than the idle one held: the recent one goes too.
    result = _collector(world, image_cache_root=tmp_path, free=GIB, **exact).collect()
    assert not recent.exists()
    assert result.images == 1


def test_an_unavailable_image_store_is_no_image_items_this_pass_then_heals(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches the guarded image store's unavailability escaping the sweep as a
    failure that ends the storage lane on the first flap of the cache mount."""

    path = _receipt(tmp_path, AN_IMAGE)
    now = [NOW]
    registry = CapabilityRegistry(clock=lambda: now[0])
    mounted = False

    def build() -> FilesystemRuntimeImageStorage:
        if not mounted:
            raise OSError("image cache mount is stale")
        return FilesystemRuntimeImageStorage(tmp_path)

    images = registry.guard(
        ControllerCapability.RUNTIME_IMAGE_STORAGE, FilesystemRuntimeImageStorage, build
    )
    exact = {"reserve_floor_bytes": 0, "reserve_fraction": 0.0}

    def sweep():
        return _collector(
            world,
            image_cache_root=tmp_path,
            images=images,
            free=100 * GIB - 1,
            **exact,
        ).collect()

    assert sweep().images == 0  # returns, no raise; nothing is removed
    assert path.exists()
    mounted = True
    now[0] += timedelta(seconds=120)  # the capability's own backoff has passed
    assert sweep().images == 1  # the next pass drives the retry and sees images
    assert not path.exists()


def test_an_image_with_a_damaged_manifest_is_evictable_with_its_on_disk_size(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches a receipt over an unservable image holding 0 bytes forever, so
    that eviction can neither count nor remove it."""

    path = _receipt(tmp_path, AN_IMAGE)
    storage = FilesystemRuntimeImageStorage(tmp_path)
    storage.layout.blob_path(f"sha256:{AN_IMAGE}").write_bytes(b'{"mediaType": "tr')
    exact = {"reserve_floor_bytes": 0, "reserve_fraction": 0.0}

    result = _collector(
        world, image_cache_root=tmp_path, free=100 * GIB - 1, **exact
    ).collect()

    assert not path.exists()
    assert result.images == 1
