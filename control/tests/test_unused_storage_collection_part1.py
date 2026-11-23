from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    LifecycleState,
    RunState,
)
from vonk_control.catalog.revision_collection import operation_tokens
from vonk_control.fleet_profile_contract import (
    FleetProfilePreview,
)
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Base,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
    FleetProfileSelection,
    InstallationNode,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.recipe_update_notice import recipe_update_notice
from vonk_control.storage_demands import (
    NAS_MODELS,
    StorageDemands,
    spark_scope,
)
from vonk_control.unused_storage_collection import (
    ACTOR,
    GRACE,
    UnusedStorageCollector,
    spark_eviction_capacity,
)
from vonk_control.unused_storage_collection import pressure as unused_storage_pressure
from vonk_control.unused_storage_collection.evidence import _Evidence

from . import unused_storage_collection_support as _support
from .test_catalog_revision_collection import NODE, NOW, OLD, Catalog
from .test_model_cache import _artifact, _download
from .unused_storage_collection_support import (
    _REAL_CLOCK,
    AN_IMAGE,
    GIB,
    TOTAL,
    FakeLifecycle,
    _application,
    _collector,
    _hard_linked_models,
    _kept,
    _model_objects,
    _models,
    _ordinals,
    _profile,
    _real_collector,
    _real_lifecycle,
    _receipt,
    _report_disk,
    _required,
    _scope,
    _size,
    _three_idle,
    _within_grace,
)

cached = _support.cached
world = _support.world


def _queued_load(world: Catalog, installation_id: str) -> None:
    """A load that has been accepted and names this exact installation."""
    from vonk_agent_protocol import AgentOperation as OperationKind
    from vonk_control.jobs.documents import RecipeInstallParent

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


def test_installation_nothing_uses_is_uninstalled_and_a_superseded_one_goes_too(
    world: Catalog,
) -> None:
    """Catches a pass that never uninstalls, or only the newest revision's."""

    old = world.revision("glm", 1)
    head = world.revision("glm", 2, head="active")
    stale, _ = world.workload(old, run="stopped")
    unpointed, _ = world.workload(head, run="stopped")
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert sorted(lifecycle.removed) == sorted([stale, unpointed])
    assert result.installations == 2


def test_old_non_model_items_expire_without_disk_pressure(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches retaining non-model assets indefinitely on a roomy filesystem."""
    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    idle, _ = world.workload(
        old, run=RunState.STOPPED, touched=NOW - timedelta(days=30)
    )
    receipt = _receipt(tmp_path, AN_IMAGE)
    lifecycle = FakeLifecycle(world.sessions, apply=True)
    result = _collector(
        world, lifecycle, free=TOTAL // 2, image_cache_root=tmp_path
    ).collect()
    assert (result.installations, result.images, result.models) == (1, 1, 0)
    assert lifecycle.removed == [idle]
    assert not receipt.exists()


def test_the_least_recently_used_go_first_and_only_as_many_as_the_shortfall_needs(
    world: Catalog,
) -> None:
    """Catches evicting everything unused (or the newest first) for a small gap."""

    oldest, middle, newest = _three_idle(world, newest_age=timedelta(days=2))
    lifecycle = FakeLifecycle(world.sessions)

    # 90 GiB free of 1000: 10 GiB under the 100 GiB low line, so 10 GiB plus the
    # 20 GiB reserve is needed and the oldest 80 GiB covers it.
    _within_grace(world)
    result = _collector(world, lifecycle, free=90 * GIB).collect()

    assert lifecycle.removed == [oldest]
    assert middle not in lifecycle.removed and newest not in lifecycle.removed
    scope = result.scopes[0]
    assert scope["scope"] == spark_scope(NODE)
    assert scope["needed_bytes"] == 10 * GIB
    assert scope["estimated_freed_bytes"] == 80 * GIB


def test_unused_model_files_the_spark_frees_itself_are_not_uninstalled_for(
    world: Catalog,
) -> None:
    """Catches uninstalling installations for space the Spark's agent frees
    itself (its unused model files) before a claim writes."""

    oldest, _middle, _newest = _three_idle(world, newest_age=timedelta(days=2))
    lifecycle = FakeLifecycle(world.sessions)

    # 90 GiB free is 10 GiB under the line; 40 GiB of unused model files
    # cover it and the reserve.
    _within_grace(world)
    _collector(world, lifecycle, free=90 * GIB, unused_models=40 * GIB).collect()
    assert lifecycle.removed == []

    # Only a need beyond them uninstalls, and only as much as that needs.
    _collector(world, lifecycle, free=90 * GIB, unused_models=5 * GIB).collect()
    assert lifecycle.removed == [oldest]


def test_a_load_is_told_its_space_is_there_when_unused_model_files_cover_it(
    world: Catalog,
) -> None:
    _three_idle(world, newest_age=timedelta(days=2))
    collector = _collector(world, free=90 * GIB, unused_models=40 * GIB)

    relief = collector.relief_for_spark(
        NODE, 120 * GIB, source="test", subject="load", reason="install"
    )

    # Nothing to wait for: admission counts the same bytes.
    assert relief is None


def test_items_used_in_the_last_day_go_only_after_everything_older(
    world: Catalog,
) -> None:
    """Catches ordering that treats a just-used installation like an idle one."""

    oldest, middle, recent = _three_idle(world, newest_age=timedelta(hours=2))
    lifecycle = FakeLifecycle(world.sessions)

    # Two of the three are needed: the recent one is the one left.
    _collector(world, lifecycle, free=GIB).collect()

    assert sorted(lifecycle.removed) == sorted([oldest, middle])
    assert recent not in lifecycle.removed


def test_an_item_used_in_the_last_day_goes_when_nothing_older_can_cover_it(
    world: Catalog,
) -> None:
    """A recent installation is last, not protected: a refused load still fits."""

    head = world.revision("glm", 1, head="active")
    recent, _ = world.workload(head, run="stopped", touched=NOW - timedelta(hours=2))
    _size(world, recent, 80 * GIB)
    lifecycle = FakeLifecycle(world.sessions)

    _collector(world, lifecycle).collect()

    assert lifecycle.removed == [recent]


def test_the_low_line_is_a_tenth_of_the_disk_or_the_largest_install_known(
    world: Catalog,
) -> None:
    """Catches a fixed low line: a Spark that cannot take its largest known
    install again is short even with plenty of percent free."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    idle, _ = world.workload(old, run="stopped")
    _size(world, idle, 80 * GIB)
    big, _ = world.workload(old, run="running")
    with world.sessions.begin() as session:
        for node in session.scalars(
            select(InstallationNode).where(InstallationNode.installation_id == big)
        ):
            node.required_bytes = 50 * GIB
    lifecycle = FakeLifecycle(world.sessions)

    # 150 GiB free (15 %) with a largest install of 50 GiB: not short.
    _within_grace(world)
    _collector(world, lifecycle, free=150 * GIB).collect()
    assert lifecycle.removed == []

    with world.sessions.begin() as session:
        for node in session.scalars(
            select(InstallationNode).where(InstallationNode.installation_id == big)
        ):
            node.required_bytes = 200 * GIB
    # The same disk cannot take a 200 GiB install: short.
    _collector(world, lifecycle, free=150 * GIB).collect()
    assert lifecycle.removed == [idle]


def test_installation_a_saved_profile_points_to_goes_after_the_unpointed(
    world: Catalog,
) -> None:
    """Space-driven cleanup may evict what a saved (not loaded) profile points
    to, but only after every installation no profile points to."""

    old = world.revision("glm", 1)
    head = world.revision("glm", 2, head="active")
    pointed, _ = world.workload(head, run="stopped", touched=NOW - timedelta(days=30))
    older, _ = world.workload(old, run="stopped", touched=NOW - timedelta(days=2))
    _profile(world, "vonk-forge/glm")
    lifecycle = FakeLifecycle(world.sessions)

    _collector(world, lifecycle).collect()

    # Profiles follow the newest revision: the older installation is no one's, so
    # it goes first although it was used more recently.
    assert lifecycle.removed[0] == older
    assert pointed in lifecycle.removed


@pytest.mark.usefixtures("damaged_json_rows")
def test_installation_of_the_loaded_profile_is_never_evicted(
    world: Catalog,
) -> None:
    """The selected (loaded) profile's installations stay, however idle."""

    head = world.revision("glm", 1, head="active")
    loaded, _ = world.workload(head, run="stopped", touched=NOW - timedelta(days=40))
    _profile(world, "vonk-forge/glm")
    _application(world, {}, state="succeeded", selected=True)
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "profile") == 1
    with world.sessions() as session:
        assert spark_eviction_capacity(session, NODE, NOW).freeable == 0
    assert loaded


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_reverted_recipe_keeps_the_head_installation_the_loaded_profile_runs(
    world: Catalog,
) -> None:
    """After A, B, A the head is A while B stays active with the higher number:
    the loaded profile's installation of A is kept, the reverted B is not."""

    head = world.revision("glm", 1, head="active")
    reverted = world.revision("glm", 2)
    loaded, _ = world.workload(head, run="stopped", touched=NOW - timedelta(days=40))
    stale, _ = world.workload(reverted, run="stopped", touched=NOW - timedelta(days=40))
    _profile(world, "vonk-forge/glm")
    _application(world, {}, state="succeeded", selected=True)
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert loaded not in lifecycle.removed
    assert stale in lifecycle.removed
    assert _kept(result, "profile") == 1


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_release_that_runs_the_same_keeps_the_loaded_installation(
    world: Catalog,
) -> None:
    """Catches a metadata-only release (new content digest, same execution key)
    costing the loaded profile's idle installation its protection."""

    same = "e" * 64
    installed = world.revision("glm", 1, head="active", execution_key=same)
    idle, _ = world.workload(installed, run="stopped", touched=NOW - timedelta(days=40))
    _profile(world, "vonk-forge/glm")
    _application(world, {}, state="succeeded", selected=True)
    world.revision("glm", 2, head="active", execution_key=same)
    lifecycle = FakeLifecycle(world.sessions)

    _collector(world, lifecycle, free=TOTAL // 2).collect()
    assert lifecycle.removed == []

    # A release that runs something else: nothing references it any more.
    world.revision("glm", 3, head="active")
    _collector(world, lifecycle, free=TOTAL // 2).collect()
    assert lifecycle.removed == [idle]


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_reload_onto_an_identical_head_lets_the_old_twin_go(
    world: Catalog,
) -> None:
    """Catches a spare installation of an identically running older revision
    kept for ever once a reload installed the head on the same Sparks."""

    same = "e" * 64
    installed = world.revision("glm", 1, head="active", execution_key=same)
    twin, _ = world.workload(installed, run="stopped", touched=NOW - timedelta(days=40))
    _profile(world, "vonk-forge/glm")
    _application(world, {}, state="succeeded", selected=True)
    head = world.revision("glm", 2, head="active", execution_key=same)
    lifecycle = FakeLifecycle(world.sessions)

    world.workload(head, run="stopped", touched=NOW - timedelta(days=40))
    _collector(world, lifecycle, free=TOTAL // 2).collect()

    assert lifecycle.removed == [twin]


def _flip_option_default(document: dict[str, Any]) -> None:
    for choice in document["options"][0]["choices"]:
        choice["default"] = not choice.get("default", False)


def _move_model_mount(document: dict[str, Any]) -> None:
    document["models"][0]["files"][0]["mount"]["target"] = "/weights"


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize(
    ("change", "bundles"),
    [
        # Options are merged into the runtime by ``with_option_choices``.
        (_flip_option_default, (None, None)),
        # A Model selection's files say where the weights are mounted.
        (_move_model_mount, (None, None)),
        # The image is built from the build context (Dockerfiles, run.py).
        (None, ("1" * 64, "2" * 64)),
    ],
    ids=["options", "model-files", "build-context"],
)
def test_a_release_that_changes_what_runs_outside_the_key_is_an_update(
    world: Catalog,
    change: Callable[[dict[str, Any]], None] | None,
    bundles: tuple[str | None, str | None],
) -> None:
    """Catches a runtime change the execution key does not cover being treated
    as metadata: the release must offer the reload, and the old idle
    installation is no longer what the head runs."""

    same = "e" * 64
    first = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "recipe-options.json")
        .read_text()
    )
    second = copy.deepcopy(first)
    if change is not None:
        change(second)
    installed = world.revision(
        "glm",
        1,
        head="active",
        execution_key=same,
        document=first,
        source_bundle_sha256=bundles[0],
    )
    idle, _ = world.workload(installed, run="stopped", touched=NOW - timedelta(days=40))
    _profile(world, "vonk-forge/glm")
    _application(world, {}, state="succeeded", selected=True)
    head = world.revision(
        "glm",
        2,
        head="active",
        execution_key=same,
        document=second,
        source_bundle_sha256=bundles[1],
    )
    lifecycle = FakeLifecycle(world.sessions)

    _collector(world, lifecycle, free=TOTAL // 2).collect()

    assert lifecycle.removed == [idle]
    with world.sessions() as session:
        running = session.get(CatalogDocumentRevision, installed)
        newest = session.get(CatalogDocumentRevision, head)
        assert running is not None and newest is not None
        assert recipe_update_notice("GLM", running, newest) is not None


def test_pointed_installations_are_planned_least_recently_used_first(
    world: Catalog,
) -> None:
    """On a Spark where only pointed installations can go, the plan names the
    profile and takes the least recently used first."""

    head = world.revision("glm", 2, head="active")
    oldest, _ = world.workload(head, run="stopped", touched=NOW - timedelta(days=20))
    newer, _ = world.workload(head, run="stopped", touched=NOW - timedelta(days=5))
    _size(world, oldest, 30 * GIB, model="a" * 64)
    _size(world, newer, 30 * GIB, model="b" * 64)
    _profile(world, "vonk-forge/glm")

    with world.sessions() as session:
        capacity = spark_eviction_capacity(session, NODE, NOW)

    assert capacity.freeable == 60 * GIB
    assert [item.members for item in capacity.items if item.profiles] == [
        (oldest,),
        (newer,),
    ]
    from vonk_control.unused_storage_collection import _eviction_order

    assert [item.members[0] for item in _eviction_order(capacity.items)] == [
        oldest,
        newer,
    ]
    assert (
        capacity.plan(20 * GIB)
        == f"evicting {30 * GIB} bytes from saved profile Coding"
    )
    assert (
        capacity.plan(40 * GIB)
        == f"evicting {60 * GIB} bytes from saved profile Coding"
    )


def test_installation_on_other_sparks_than_the_profile_names_is_not_pointed_to(
    world: Catalog,
) -> None:
    """Catches matching a profile on the recipe alone, whatever the Sparks."""

    head = world.revision("glm", 1, head="active")
    installation, _ = world.workload(head, run="stopped")
    _profile(world, "vonk-forge/glm", sparks=["spk_" + "2" * 32])
    lifecycle = FakeLifecycle(world.sessions)

    _collector(world, lifecycle).collect()

    assert lifecycle.removed == [installation]


@pytest.mark.parametrize("state", ["running", "starting", "stopping"])
def test_installation_with_a_workload_that_is_not_stopped_is_kept(
    world: Catalog, state: str
) -> None:
    """Catches uninstalling under a running (or maybe still running) workload."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.workload(old, run=state)
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "running") == 1


@pytest.mark.parametrize("ended", ["failed", "lost"])
def test_an_installation_with_a_failed_run_first_has_that_run_stopped(
    world: Catalog, ended: str
) -> None:
    """Catches a failed or lost run pinning its installation's disk for ever: it
    counts as removable, the pass queues its Stop (proof the process is gone)
    and removes nothing else on either of its Sparks until that lands, and the
    uninstall follows."""

    other = "spk_" + "2" * 32
    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    failed, run = world.workload(old, run=ended)
    idle, _ = world.workload(old, run="stopped", touched=NOW - timedelta(days=1))
    elsewhere, _ = world.workload(old, run="stopped", touched=NOW - timedelta(days=1))
    with world.sessions.begin() as session:
        session.add(AgentNode(node_id=other, state="active", workload_intent_ordinal=1))
        session.add(
            InstallationNode(
                installation_id=failed,
                node_id=other,
                rank=1,
                role="worker",
                state="installed",
                required_bytes=1,
                installed_bytes=1,
                updated_at=OLD,
            )
        )
        moved = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == elsewhere
            )
        )
        assert moved is not None
        moved.node_id = other
    for installation in (failed, idle, elsewhere):
        _size(world, installation, 80 * GIB)
    lifecycle = FakeLifecycle(world.sessions)
    collector = _collector(world, lifecycle)
    _report_disk(world.sessions, GIB, at=NOW, node=other)

    with world.sessions() as session:
        assert spark_eviction_capacity(session, NODE, NOW).freeable == 160 * GIB
    first = collector.collect()

    assert lifecycle.stopped == [run]
    assert lifecycle.removed == []
    assert _scope(first, spark_scope(NODE))["outcome"] == "evicting"
    assert _scope(first, spark_scope(other))["outcome"] == "evicting"

    with world.sessions.begin() as session:
        stopped = session.get(RecipeRun, run)
        assert stopped is not None
        stopped.state = "stopped"
    collector._due_at = None
    collector.collect()

    assert failed in lifecycle.removed


def test_an_installation_of_a_model_a_profile_needs_is_kept(world: Catalog) -> None:
    """Removing the last installation of a model deletes the Spark's shared copy,
    so a superseded revision's installation stays while the profile's newest
    revision of the same model would have to fetch the weights again (and its
    admission counts them as already there)."""

    old = world.revision("glm", 1)
    world.revision(
        "glm",
        2,
        head="active",
        document={"models": [{"model": {"content_sha256": "c" * 64}}]},
    )
    superseded, _ = world.workload(old, run="stopped")
    _size(world, superseded, 80 * GIB, model="c" * 64)
    other, _ = world.workload(old, run="stopped")
    _size(world, other, 80 * GIB, model="d" * 64)
    _profile(world, "vonk-forge/glm")
    lifecycle = FakeLifecycle(world.sessions)

    _collector(world, lifecycle).collect()

    # Models stay on the NAS, so a profile's shared model copy on the Spark
    # may go and a later load simply reinstalls it.
    assert sorted(lifecycle.removed) == sorted([superseded, other])


def test_a_profile_edit_or_a_new_revision_alone_removes_nothing(
    world: Catalog,
) -> None:
    """The owner's rule: only a lack of space removes anything, so an edit or a
    superseding revision neither removes nor (when short) protects."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    superseded, _ = world.workload(old, run="stopped")
    _profile(world, "vonk-forge/other")
    with world.sessions.begin() as session:
        for profile in session.scalars(select(FleetProfile)):
            profile.updated_at = NOW - timedelta(minutes=5)
    lifecycle = FakeLifecycle(world.sessions)

    _within_grace(world)
    _collector(world, lifecycle, free=TOTAL // 2).collect()
    assert lifecycle.removed == []

    _collector(world, lifecycle).collect()
    assert lifecycle.removed == [superseded]


def test_installations_of_one_model_go_together_or_not_at_all(world: Catalog) -> None:
    """Their model files are shared, so removing one of two frees nothing."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    first, _ = world.workload(old, run="stopped")
    second, _ = world.workload(old, run="stopped")
    _size(world, first, 80 * GIB, model="c" * 64)
    _size(world, second, 70 * GIB, model="c" * 64)
    lifecycle = FakeLifecycle(world.sessions)

    _within_grace(world)
    result = _collector(world, lifecycle).collect()

    assert sorted(lifecycle.removed) == sorted([first, second])
    # The shared bytes are counted once, not once per installation.
    assert result.scopes[0]["estimated_freed_bytes"] == 80 * GIB

    # One of them still in use keeps the model, so removing the other frees none.
    other = FakeLifecycle(world.sessions)
    with world.sessions.begin() as session:
        run = session.scalar(
            select(RecipeRun).where(RecipeRun.installation_id == second)
        )
        assert run is not None
        run.state = "running"
    result = _collector(world, other).collect()
    assert other.removed == []
    assert _kept(result, "model shared with one in use") == 1


@pytest.mark.usefixtures("damaged_json_rows")
def test_shared_model_files_free_only_with_the_last_installation_that_links_them(
    world: Catalog,
) -> None:
    """Catches counting a hard-linked file once per installation (or per model):
    removing both models frees A + B + C and each model's own bytes, not the sum
    of the installations' sizes."""

    _hard_linked_models(world)
    _collector(world, free=400 * GIB)

    with world.sessions() as session:
        capacity = spark_eviction_capacity(session, NODE, world.now)

    # 40 + 60 + 30 for the files and 10 for each model's own bytes. Counting a
    # model's installed size per model gives 110 + 100 = 210 (850 per
    # installation): B would be freed twice.
    assert capacity.freeable == 150 * GIB
    # B is shared by M1 and M2: the first model out frees only what no other
    # model links, the second takes B with it.
    first, second = capacity.items
    assert len(first.members) == 5 and first.freeable == 10 * GIB + 40 * GIB
    assert (
        len(second.members) == 3 and second.freeable == 10 * GIB + 60 * GIB + 30 * GIB
    )


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_shared_file_a_staying_installation_links_is_not_counted_as_freed(
    world: Catalog,
) -> None:
    """M2 is running, so B stays: removing M1 frees A and its own bytes only."""

    _first, second = _hard_linked_models(world)
    with world.sessions.begin() as session:
        run = session.scalar(
            select(RecipeRun).where(RecipeRun.installation_id == second[0])
        )
        assert run is not None
        run.state = "running"
    demands = StorageDemands(lambda: world.now)
    lifecycle = FakeLifecycle(world.sessions)
    _within_grace(world)
    collector = _collector(world, lifecycle, free=400 * GIB, demands=demands)

    with world.sessions() as session:
        capacity = spark_eviction_capacity(session, NODE, world.now)
    assert capacity.freeable == 50 * GIB
    assert [len(item.members) for item in capacity.items] == [5]

    # A load needing 100 GiB more than free cannot be met by the 50 GiB that
    # removing M1 really frees (the naive estimate promised 550): nothing is
    # removed for it, and it is told how much could be freed.
    relief = collector.relief_for_spark(
        NODE, 500 * GIB, source="profile-load", subject="p", reason="x"
    )
    result = collector.collect()
    assert relief is not None
    assert relief.freeable_bytes == 50 * GIB
    assert lifecycle.removed == []
    assert _scope(result, spark_scope(NODE))["outcome"] == "insufficient_after_eviction"


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_model_that_only_shares_files_with_one_that_stays_is_not_offered(
    world: Catalog,
) -> None:
    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    kept, _ = world.workload(old, run="running")
    other, _ = world.workload(old, run="stopped")
    first = _model_objects(world, "1" * 64, {"a" * 64: 50 * GIB})
    second = _model_objects(world, "2" * 64, {"a" * 64: 50 * GIB})
    _size(world, kept, 50 * GIB, model="1" * 64, model_set=first)
    _size(world, other, 50 * GIB, model="2" * 64, model_set=second)
    lifecycle = FakeLifecycle(world.sessions)

    _within_grace(world)
    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "shares its files with one that stays") == 1


def test_a_redigested_model_shares_its_files_with_the_old_digest_that_stays(
    world: Catalog,
) -> None:
    """Catches finding a set's files only through the Model that first requested
    it: a release re-digested the Model (same files), so an installation of the
    new digest runs the same set and links the files a running one of the old
    digest holds."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    kept, _ = world.workload(old, run="running")
    other, _ = world.workload(old, run="stopped")
    shared = _model_objects(world, "1" * 64, {"a" * 64: 50 * GIB})
    _size(world, kept, 50 * GIB, model="1" * 64, model_set=shared)
    _size(world, other, 50 * GIB, model="2" * 64, model_set=shared)
    lifecycle = FakeLifecycle(world.sessions)

    _within_grace(world)
    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "shares its files with one that stays") == 1


@pytest.mark.usefixtures("damaged_json_rows")
def test_admission_relief_removes_the_installations_of_a_model_together_and_frees_real_bytes(
    world: Catalog,
) -> None:
    """The pass asked for 140 GiB more than is free removes M1 (50) then M2
    (100 more), every installation of a model together, each told which others
    go with it so the Spark reclaims the files the last one holds."""

    first, second = _hard_linked_models(world)
    demands = StorageDemands(lambda: world.now)
    lifecycle = FakeLifecycle(world.sessions)
    collector = _collector(world, lifecycle, free=400 * GIB, demands=demands)
    demands.request(
        spark_scope(NODE),
        540 * GIB,
        source="profile-load",
        subject="p",
        reason="run-switch.insufficient-disk",
    )

    result = collector.collect()

    assert sorted(lifecycle.removed) == sorted([*first, *second])
    # Only 10 GiB per model is removed by uninstall. Its 130 GiB of shared
    # objects remain in the agent's LRU, so they must not trigger a false pause.
    assert _scope(result, spark_scope(NODE))["estimated_freed_bytes"] == 20 * GIB
    for group in (first, second):
        for installation in group:
            assert lifecycle.also_removing[installation] == tuple(
                sorted(item for item in group if item != installation)
            )


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_load_waiting_to_be_admitted_does_not_keep_what_it_waits_for_space_from(
    world: Catalog,
) -> None:
    """Catches the deadlock a parked load made of itself: its plan names both
    Sparks, so every installation there counted as a live operation, nothing was
    evictable and the load waited for ever for the space it kept."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    installation, _ = world.workload(old, run="stopped")
    _size(world, installation, 80 * GIB)
    _application(
        world,
        {"steps": [{"node_ids": [NODE]}]},
        state="queued",
        canonical=True,
        progress={
            "admission_pending": True,
            "admission_attempt": 86,
            "blockers": [
                {
                    "code": "profile.admission_busy",
                    "detail": "Profile resource admission is waiting for capacity",
                    "severity": "warning",
                    "node_ids": [],
                }
            ],
        },
    )
    lifecycle = FakeLifecycle(world.sessions)

    _collector(world, lifecycle).collect()

    assert lifecycle.removed == [installation]


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_review_can_evict_without_superseding_an_unrelated_load(
    world: Catalog,
) -> None:
    """Catches a review that refused a load for disk the load it replaces held:
    a sweep's previous load, still copying to the other Spark, kept every
    installation on this one as a live operation, so 88 GB that the new load
    could evict counted as kept and it was refused for 8 GB."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    installation, _ = world.workload(old, run="stopped")
    _size(world, installation, 80 * GIB)
    previous = _application(
        world,
        {},
        state="running",
        canonical=True,
        operation=str(uuid.uuid4()),
        progress={
            "admission_pending": False,
            "blockers": [],
            "switch_adapter": {
                "children": [],
                "pending_children": [
                    {"queue_index": 1, "operation_id": str(uuid.uuid4()), "kind": "run"}
                ],
            },
        },
    )

    with world.sessions() as session:
        held = spark_eviction_capacity(session, NODE, NOW)
        replaced = spark_eviction_capacity(session, NODE, NOW, (previous,))

    assert held.freeable == 80 * GIB
    assert replaced.freeable == 80 * GIB


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_load_a_review_supersedes_names_nothing_for_it(world: Catalog) -> None:
    old = world.revision("glm", 1)
    installation, _ = world.workload(old, run="stopped")
    previous = _application(
        world,
        {"effects": {"installations": [{"installation_id": installation}]}},
        state="running",
    )

    with world.sessions() as session:
        named = operation_tokens(session, NOW, recent=False)
        replaced = operation_tokens(
            session, NOW, recent=False, superseded_applications=(previous,)
        )

    assert installation in named
    assert installation not in replaced


@pytest.mark.usefixtures("damaged_json_rows")
def test_installation_a_live_operation_names_is_kept(world: Catalog) -> None:
    """Catches ignoring a parked load that plans to reuse the installation."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    installation, _ = world.workload(old, run="stopped")
    world.job(
        {"plan": {"installation_id": installation}},
        state="observing",
        updated=NOW - timedelta(days=3),
    )
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "live operation") == 1


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
    from vonk_control.fleet_profile_contract import FleetProfileInstallationEffect

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
    from vonk_agent_protocol import WaitReason
    from vonk_control.recipe_operations import RecipeRetryLater

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
    from vonk_control.persisted_waits import read_wait

    with world.sessions() as session:
        retained = session.get(RecipeInstallation, first)
        assert retained is not None and read_wait(retained) is not None
    collector = _collector(world, lifecycle, now=world.now)
    _report_disk(world.sessions, GIB + 2, at=world.now)
    collector.collect()
    assert calls.count(first) == 1, "unknown outcome retried without backoff"
    world.now += collector._scan_interval
    collector.collect()
    assert calls.count(first) == 2 and first in lifecycle.removed
    with world.sessions() as session:
        settled = session.get(RecipeInstallation, first)
        assert settled is not None and read_wait(settled) is None


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


def _agents_succeed(sessions: sessionmaker[Session], service, job_id: str) -> None:
    """Every Spark reports success for each step of the job, phase by phase."""

    while service.get(job_id).state == "running":
        with sessions() as session:
            pending = [
                child.node_id
                for child in session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.parent_job_id == job_id,
                        AgentOperation.state == "queued",
                    )
                )
            ]
        assert pending
        for node_id in pending:
            service.record_node_result(job_id, node_id, succeeded=True, evidence={})


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

    from vonk_control.distributed_recovery.common import (
        _settle_unrecoverable,
        release_inactive_run_claims_in_session,
    )

    from .test_recipe_operations import started_recipe

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
    assert held is not None and held > 0
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
