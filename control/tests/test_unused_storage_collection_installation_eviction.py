"""Unused storage collection: installation eviction."""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable
from datetime import timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    RunState,
)
from vonk_control.catalog.revision_collection import operation_tokens
from vonk_control.models.catalog import (
    CatalogDocumentRevision,
)
from vonk_control.models.execution import (
    InstallationNode,
    RecipeRun,
)
from vonk_control.models.fleet import (
    AgentNode,
    FleetProfile,
)
from vonk_control.recipe_update_batches.notice import recipe_update_notice
from vonk_control.storage_demands import (
    StorageDemands,
    spark_scope,
)
from vonk_control.unused_storage_collection.common import spark_eviction_capacity

from .test_catalog_revision_collection import NODE, NOW, OLD, Catalog
from .unused_storage_collection_support import (
    AN_IMAGE,
    GIB,
    TOTAL,
    FakeLifecycle,
    _application,
    _collector,
    _hard_linked_models,
    _kept,
    _model_objects,
    _profile,
    _receipt,
    _report_disk,
    _scope,
    _size,
    _three_idle,
    _within_grace,
)

# -- installations ----------------------------------------------------------------


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
    from vonk_control.unused_storage_collection.common import _eviction_order

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
