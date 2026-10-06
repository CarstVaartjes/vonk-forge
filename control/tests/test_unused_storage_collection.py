"""Unused installations, image receipts and cached models are removed to make room.

They are removed only while disk is short (work was refused for lack of it, or
free space is below the low line), least recently used first and no more than
the shortfall needs, and are kept while a saved profile points to them, a
workload runs, or a live operation names them. A load that starts after the
pass looked is never raced.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.artifact_lifecycle import ArtifactLifecycleGate
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    AgentNode,
    ArtifactDistributionAssignment,
    Base,
    FleetProfile,
    FleetProfileApplication,
    FleetProfileSelection,
    InstallationNode,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    NodeInventorySnapshot,
    RecipeInstallation,
    RecipeRun,
)
from vonk_control.recipe_operations import RecipeOperationConflict
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_control.storage_demands import (
    NAS_MODELS,
    STORAGE_EVICTING,
    STORAGE_INSUFFICIENT,
    StorageDemands,
    spark_scope,
)
from vonk_control.unused_storage_collection import (
    ACTOR,
    GRACE,
    UnusedStorageCollector,
    spark_eviction_capacity,
)

from .runtime_image_fixtures import place_test_image
from .test_catalog_revision_collection import NODE, NOW, OLD, Catalog
from .test_model_cache import _artifact, _download

A_MODEL = "a" * 64
AN_IMAGE = "d" * 64
GIB = 1024**3
TOTAL = 1000 * GIB


@pytest.fixture
def world(tmp_path: Path) -> Catalog:
    engine = create_engine(f"sqlite:///{tmp_path / 'controller.sqlite'}")

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return Catalog(engine)


class FakeLifecycle:
    """Records the uninstalls the sweep asks for, running its guard as the real
    one does: in a transaction, before anything is queued."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        between: Callable[[str], None] | None = None,
        refuse: frozenset[str] = frozenset(),
        apply: bool = False,
    ) -> None:
        self._sessions = sessions
        self._between = between
        self._refuse = refuse
        self._apply = apply
        self.removed: list[str] = []
        self.also_removing: dict[str, tuple[str, ...]] = {}

    def preview_uninstall(self, installation_id: str, *, also_removing=()):
        # The sweep has judged the installation unused; a load can still
        # arrive before the removal is queued.
        if self._between is not None:
            self._between(installation_id)
        return SimpleNamespace(allowed=True, blockers=(), plan_digest="f" * 64)

    def uninstall(
        self,
        installation_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        unattended_guard: Callable[[Session], None] | None = None,
        also_removing=(),
    ) -> None:
        assert actor == ACTOR and unattended_guard is not None
        self.also_removing[installation_id] = tuple(sorted(also_removing))
        if installation_id in self._refuse:
            raise RecipeOperationConflict("refused")
        with self._sessions.begin() as session:
            unattended_guard(session)
            if self._apply:
                installation = session.get(RecipeInstallation, installation_id)
                assert installation is not None
                installation.state = "uninstalled"
        self.removed.append(installation_id)


def _report_disk(
    sessions: sessionmaker[Session],
    free: int,
    *,
    at: datetime,
    node: str = NODE,
    total: int = TOTAL,
) -> None:
    """What a Spark's inventory reports for its disk, as of ``at``."""

    stamp = at
    with sessions.begin() as session:
        existing = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == node,
                NodeInventorySnapshot.observed_at == stamp,
            )
        )
        if existing is not None:
            existing.disk_free_bytes = free
            return
        session.add(
            NodeInventorySnapshot(
                node_id=node,
                observed_at=stamp,
                received_at=stamp,
                disk_total_bytes=total,
                disk_free_bytes=free,
                host_memory_total_bytes=1,
                host_memory_free_bytes=1,
                gpu_memory_total_bytes=1,
                gpu_memory_free_bytes=1,
                gpu_count=1,
                memory_pool="separate",
                artifact_store_read_only=False,
                capabilities=[],
                evidence_digest=uuid.uuid4().hex + uuid.uuid4().hex,
            )
        )


def _collector(
    world: Catalog,
    lifecycle: FakeLifecycle | None = None,
    *,
    now: datetime | None = None,
    free: int = GIB,
    **options,
) -> UnusedStorageCollector:
    """A collector for a Spark and a NAS that both report ``free`` bytes of a
    1000 GiB disk: by default 1 GiB, far below the 10 % low line."""

    world.now = now or NOW
    _report_disk(world.sessions, free, at=world.now)
    options.setdefault("disk_usage", lambda _path: (TOTAL, free))
    return UnusedStorageCollector(
        world.sessions,
        clock=lambda: world.now,
        lifecycle=lifecycle or FakeLifecycle(world.sessions),  # type: ignore[arg-type]
        **options,
    )


def _profile(world: Catalog, selector: str, sparks: list[str] | None = None) -> None:
    """A saved profile that assigns ``selector`` to the given Sparks."""

    with world.sessions.begin() as session:
        session.add(
            FleetProfile(
                number=1,
                name="Coding",
                installation_policy="keep-cached",
                assignments=[
                    {
                        "recipe_selector": selector,
                        "spark_ids": sparks or [NODE],
                        "desired_state": "running",
                        "option_choices": {},
                    }
                ],
                created_by="test",
                created_at=OLD,
                updated_at=OLD,
            )
        )


def _queued_load(world: Catalog) -> None:
    """A load that has been accepted and targets the Spark."""

    with world.sessions.begin() as session:
        session.add(
            Job(
                request_id=str(uuid.uuid4()),
                kind="recipe.run-switch.v2",
                state="queued",
                actor="test",
                authority_revision="r",
                targets=[NODE],
                payload_digest="0" * 64,
                payload={},
                current_attempt=0,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def _kept(result, reason: str) -> int:
    return sum(count for key, count in result.kept.items() if reason in key)


def _scope(result, scope: str) -> dict[str, object]:
    return next(item for item in result.scopes if item["scope"] == scope)


def _size(
    world: Catalog, installation_id: str, size: int, *, model: str | None = None
) -> None:
    """What the installation holds on its Spark, and optionally its model."""

    with world.sessions.begin() as session:
        for node in session.scalars(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation_id
            )
        ):
            node.installed_bytes = size
        if model is not None:
            row = session.get(RecipeInstallation, installation_id)
            assert row is not None
            row.model_content_sha256 = model


def _unattended_uninstall(world: Catalog, *, state: str, updated: datetime) -> None:
    """An uninstall this collector queued earlier, as the real lifecycle records it."""

    with world.sessions.begin() as session:
        session.add(
            Job(
                request_id=str(uuid.uuid4()),
                kind="recipe.uninstall",
                state=state,
                actor=ACTOR,
                authority_revision="r",
                targets=[NODE],
                payload_digest="0" * 64,
                payload={},
                current_attempt=0,
                created_at=updated,
                updated_at=updated,
            )
        )


def _three_idle(world: Catalog, *, newest_age: timedelta) -> tuple[str, str, str]:
    """Three unused 80 GiB installations, last used 9, 5 and ``newest_age`` ago."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    found = []
    for age in (timedelta(days=9), timedelta(days=5), newest_age):
        installation, _ = world.workload(old, run="stopped", touched=NOW - age)
        _size(world, installation, 80 * GIB)
        found.append(installation)
    return found[0], found[1], found[2]


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


def test_nothing_is_removed_while_disk_is_not_short(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches time-based removal: an item idle for a month holds disk nobody is
    waiting for, so it stays however much time passes."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.workload(old, run="stopped", touched=NOW - timedelta(days=30))
    receipt = _receipt(tmp_path, AN_IMAGE)
    lifecycle = FakeLifecycle(world.sessions)

    for now in (NOW, NOW + timedelta(days=90)):
        result = _collector(
            world, lifecycle, now=now, free=TOTAL // 2, image_cache_root=tmp_path
        ).collect()
        assert (result.installations, result.images, result.models) == (0, 0, 0)

    assert lifecycle.removed == []
    assert receipt.exists()


def test_the_least_recently_used_go_first_and_only_as_many_as_the_shortfall_needs(
    world: Catalog,
) -> None:
    """Catches evicting everything unused (or the newest first) for a small gap."""

    oldest, middle, newest = _three_idle(world, newest_age=timedelta(days=2))
    lifecycle = FakeLifecycle(world.sessions)

    # 90 GiB free of 1000: 10 GiB under the 100 GiB low line, so 10 GiB plus the
    # 20 GiB reserve is needed and the oldest 80 GiB covers it.
    result = _collector(world, lifecycle, free=90 * GIB).collect()

    assert lifecycle.removed == [oldest]
    assert middle not in lifecycle.removed and newest not in lifecycle.removed
    scope = result.scopes[0]
    assert scope["scope"] == spark_scope(NODE)
    assert scope["needed_bytes"] == 10 * GIB
    assert scope["estimated_freed_bytes"] == 80 * GIB


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


@pytest.mark.parametrize("state", ["running", "starting", "stopping", "lost"])
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


def test_installation_with_a_failed_run_waits_for_the_run_to_be_stopped(
    world: Catalog,
) -> None:
    """Uninstall refuses an unstopped run; the sweep does not stop one itself."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.workload(old, run="failed")
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "run not stopped") == 1


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


def _model_objects(world: Catalog, model: str, objects: dict[str, int]) -> None:
    """The model files the cache recorded for a model: file digest -> bytes."""

    with world.sessions.begin() as session:
        session.add(
            ModelCacheSet(
                artifact_set_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                schema_version=2,
                model_content_sha256=model,
                manifest={
                    "schema_version": 2,
                    "artifacts": [
                        {
                            "key": digest[:8],
                            "sha256": digest,
                            "download_bytes": size,
                            "model_content_sha256": model,
                        }
                        for digest, size in sorted(objects.items())
                    ],
                },
                expected_bytes=sum(objects.values()),
                verified_bytes=sum(objects.values()),
                state="cached",
                created_at=OLD,
                updated_at=OLD,
                last_accessed_at=OLD,
            )
        )


def _hard_linked_models(world: Catalog) -> tuple[list[str], list[str]]:
    """Five installations of model M1 and three of M2, which share one file.

    M1 = A (40) + B (60) and M2 = B (60) + C (30); each installation holds 10
    bytes of its own besides the linked files. All idle, M1 used less recently.
    """

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    m1, m2 = "1" * 64, "2" * 64
    _model_objects(world, m1, {"a" * 64: 40 * GIB, "b" * 64: 60 * GIB})
    _model_objects(world, m2, {"b" * 64: 60 * GIB, "c" * 64: 30 * GIB})
    first: list[str] = []
    second: list[str] = []
    for model, installed, found, age in (
        (m1, 110 * GIB, first, timedelta(days=9)),
        (m1, 110 * GIB, first, timedelta(days=9)),
        (m1, 110 * GIB, first, timedelta(days=9)),
        (m1, 110 * GIB, first, timedelta(days=9)),
        (m1, 110 * GIB, first, timedelta(days=9)),
        (m2, 100 * GIB, second, timedelta(days=5)),
        (m2, 100 * GIB, second, timedelta(days=5)),
        (m2, 100 * GIB, second, timedelta(days=5)),
    ):
        installation, _ = world.workload(old, run="stopped", touched=NOW - age)
        _size(world, installation, installed, model=model)
        found.append(installation)
    return first, second


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
    assert relief is not None and relief.code == STORAGE_INSUFFICIENT
    assert relief.freeable_bytes == 50 * GIB
    assert "stay because its workload is running" in relief.detail
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
    _model_objects(world, "1" * 64, {"a" * 64: 50 * GIB})
    _model_objects(world, "2" * 64, {"a" * 64: 50 * GIB})
    _size(world, kept, 50 * GIB, model="1" * 64)
    _size(world, other, 50 * GIB, model="2" * 64)
    lifecycle = FakeLifecycle(world.sessions)

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
    assert _scope(result, spark_scope(NODE))["estimated_freed_bytes"] == 150 * GIB
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
def test_a_load_that_issued_something_still_keeps_the_installations_it_uses(
    world: Catalog,
) -> None:
    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.workload(old, run="stopped")
    _application(
        world,
        {"steps": [{"node_ids": [NODE]}]},
        state="running",
        progress={"admission_pending": False, "blockers": []},
    )
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "live operation") == 1


@pytest.mark.usefixtures("damaged_json_rows")
def test_installation_a_live_operation_names_is_kept(world: Catalog) -> None:
    """Catches ignoring a parked load that plans to reuse the installation."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    installation, _ = world.workload(old, run="stopped")
    world.job(
        {"plan": {"installation_id": installation}},
        state="waiting",
        updated=NOW - timedelta(days=3),
    )
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "live operation") == 1


def _application(
    world: Catalog,
    plan: dict[str, object],
    *,
    state: str,
    selected: bool = False,
    progress: dict[str, object] | None = None,
) -> None:
    with world.sessions.begin() as session:
        profile = session.scalar(select(FleetProfile))
        if profile is None:
            _profile(world, "vonk-forge/other")
            profile = session.scalar(select(FleetProfile))
        assert profile is not None
        application = FleetProfileApplication(
            request_key=str(uuid.uuid4()),
            profile_id=profile.id,
            profile_digest="1" * 64,
            plan_digest=uuid.uuid4().hex + uuid.uuid4().hex,
            state=state,
            plan=plan,
            actor="test",
            created_at=OLD,
            updated_at=OLD,
            **({"progress": progress} if progress is not None else {}),
        )
        session.add(application)
        session.flush()
        if selected:
            session.add(
                FleetProfileSelection(
                    singleton_id=1,
                    generation=1,
                    profile_id=profile.id,
                    profile_revision=1,
                    application_id=application.id,
                    roster_digest="2" * 64,
                    updated_at=OLD,
                )
            )


@pytest.mark.usefixtures("damaged_json_rows")
def test_installation_a_profile_load_in_flight_on_its_sparks_is_kept(
    world: Catalog,
) -> None:
    """A load accepted as a profile application may not have queued a job yet."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.workload(old, run="stopped")
    _application(world, {"steps": [{"node_ids": [NODE]}]}, state="running")
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "live operation") == 1


@pytest.mark.usefixtures("damaged_json_rows")
def test_installation_the_selected_profile_application_names_is_kept(
    world: Catalog,
) -> None:
    """The selected application is what the fleet was last asked to be."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    installation, _ = world.workload(old, run="stopped")
    _application(
        world,
        {"effects": {"installations": [{"installation_id": installation}]}},
        state="succeeded",
        selected=True,
    )
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "live operation") == 1


def test_a_load_accepted_before_the_removal_is_queued_stops_the_removal(
    world: Catalog,
) -> None:
    """Catches a removal that races a load: the guard re-proves under lock."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.workload(old, run="stopped")
    lifecycle = FakeLifecycle(world.sessions, between=lambda _id: _queued_load(world))

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


@pytest.mark.usefixtures("damaged_json_rows")
def test_installation_is_kept_while_a_profile_cannot_be_read(world: Catalog) -> None:
    """Nothing is proven unused when a profile is unreadable (fail closed)."""

    head = world.revision("glm", 1, head="active")
    world.workload(head, run="stopped")
    with world.sessions.begin() as session:
        session.add(
            FleetProfile(
                number=1,
                name="Broken",
                installation_policy="exact",
                assignments=[{"recipe_selector": 7}],
                created_by="test",
                created_at=OLD,
                updated_at=OLD,
            )
        )
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "profiles unreadable") == 1


def test_installations_that_may_hold_partial_files_are_left_to_reconcile(
    world: Catalog,
) -> None:
    """Partial and failed installs belong to the exact reconcile path."""

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


def test_a_sweep_that_runs_out_of_budget_continues_on_the_next_pass(
    world: Catalog,
) -> None:
    """Catches waiting a whole interval to finish a sweep the budget cut short."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.workload(old, run="stopped")
    lifecycle = FakeLifecycle(world.sessions)
    collector = _collector(world, lifecycle, budget_seconds=-1.0)

    assert collector.tick() is False
    assert lifecycle.removed == []

    collector._budget_seconds = 30.0
    assert collector.tick() is True
    assert len(lifecycle.removed) == 1


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


# -- the real lifecycle: no new workload intent --------------------------------------


def _real_lifecycle(tmp_path: Path, *, nodes: int = 1, engine: Engine | None = None):
    from .test_recipe_operations import installed_recipe, setup_services

    sessions, service, _queue, mapping_id, build_id, node_ids = setup_services(
        tmp_path, nodes=nodes, engine=engine
    )
    installation = installed_recipe(
        service, mapping_id, build_id, node_ids, request_id=str(uuid.uuid4())
    )
    return sessions, service, installation.owner_id, node_ids


_REAL_CLOCK = datetime(2026, 8, 7, 12, tzinfo=UTC) + timedelta(days=2)


def _real_collector(
    sessions: sessionmaker[Session], service, nodes: tuple[str, ...]
) -> UnusedStorageCollector:
    """A collector over a real lifecycle whose Sparks report little free disk."""

    for node in nodes:
        _report_disk(sessions, GIB, at=_REAL_CLOCK, node=node)
    return UnusedStorageCollector(
        sessions, clock=lambda: _REAL_CLOCK, lifecycle=service
    )


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


def test_removal_is_refused_when_the_target_sparks_hold_different_intents(
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

    result = collector.collect()

    assert result.installations == 0
    assert _kept(result, "refused") == 1
    with sessions() as session:
        assert session.scalar(select(Job).where(Job.kind == "recipe.uninstall")) is None
    assert _ordinals(sessions) == before


def test_removal_a_guard_refuses_queues_nothing(tmp_path: Path) -> None:
    """The guard runs with the Sparks locked and before the job exists."""

    sessions, service, installation_id, nodes = _real_lifecycle(tmp_path)
    before = _ordinals(sessions)

    def refuse(_session: Session) -> None:
        raise RuntimeError("a load arrived")

    plan = service.preview_uninstall(installation_id)
    with pytest.raises(RuntimeError, match="a load arrived"):
        service.uninstall(
            installation_id,
            plan_digest=plan.plan_digest,
            actor=ACTOR,
            request_id=str(uuid.uuid4()),
            unattended_guard=refuse,
        )

    with sessions() as session:
        assert session.scalar(select(Job).where(Job.kind == "recipe.uninstall")) is None
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None and installation.state == "installed"
        assert session.scalar(select(func.count()).select_from(RecipeRun)) == 0
        assert session.scalar(
            select(func.count()).select_from(InstallationNode)
        ) == len(nodes)
    assert _ordinals(sessions) == before


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
            plan = service.preview_uninstall(installation_id)
            outcome["view"] = service.uninstall(
                installation_id,
                plan_digest=plan.plan_digest,
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
        with sessions() as load, pytest.raises(OperationalError):
            load.execute(
                select(AgentNode.node_id)
                .where(AgentNode.node_id.in_(nodes))
                .with_for_update(nowait=True)
            ).all()
    finally:
        released.set()
        worker.join(30)

    assert "error" not in outcome, outcome
    with sessions() as session:
        job = session.scalar(select(Job).where(Job.kind == "recipe.uninstall"))
        assert job is not None
        assert job.payload["workload_intent_ordinal"] == before[nodes[0]]
    assert _ordinals(sessions) == before


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


# -- image receipts -----------------------------------------------------------------


def _receipt(root: Path, archive: str, *, age: timedelta = timedelta(days=3)) -> Path:
    """A published receipt over a stored image of 1 KiB, last written ``age`` ago."""

    place_test_image(FilesystemRuntimeImageStorage(root), archive, 1024)
    path = root / "image-cache" / f"{archive}.receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


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


def test_receipt_a_profile_points_to_is_kept_and_a_merely_offered_one_is_not(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches removing the image the newest revision of a profiled recipe would
    load, and keeping every image the catalog offers (which would leave the NAS
    nothing to free)."""

    head = world.revision("glm", 1, head="active")
    world.build(head, archive=AN_IMAGE)
    path = _receipt(tmp_path, AN_IMAGE)

    _profile(world, "vonk-forge/glm")
    result = _collector(world, image_cache_root=tmp_path).collect()
    assert path.exists()
    assert _kept(result, "profile") == 1

    with world.sessions.begin() as session:
        for profile in session.scalars(select(FleetProfile)):
            session.delete(profile)
    result = _collector(world, image_cache_root=tmp_path).collect()
    assert not path.exists()
    assert result.images == 1


def test_receipt_of_a_superseded_revision_goes_but_a_shared_one_stays(
    world: Catalog, tmp_path: Path
) -> None:
    """The image an editorial successor reuses is the head's, so it stays.

    The head has no build row of its own: it runs the build its predecessor made.
    """

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.build(old, archive=AN_IMAGE)
    shared = _receipt(tmp_path, AN_IMAGE)
    _profile(world, "vonk-forge/glm")

    _collector(world, image_cache_root=tmp_path).collect()

    assert shared.exists()


@pytest.mark.usefixtures("damaged_json_rows")
def test_receipt_a_live_operation_names_is_kept_and_a_recent_transfer_only_goes_last(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches removing an image a load is using, and treating a recent Spark
    pull as a reason to keep it from a load that is waiting for room."""

    named = _receipt(tmp_path, "1" * 64)
    pulled = _receipt(tmp_path, "2" * 64)
    idle = _receipt(tmp_path, "7" * 64)
    world.job({"plan": {"image": "1" * 64}}, state="waiting", updated=NOW - GRACE)
    with world.sessions.begin() as session:
        session.add(
            ArtifactDistributionAssignment(
                plan_digest="3" * 64,
                node_id=NODE,
                generation=1,
                expires_at=NOW - timedelta(hours=2),
                model_artifact_set_sha256="4" * 64,
                objects=[],
                oci_image_digest="sha256:" + "5" * 64,
                oci_image_config_digest="sha256:" + "6" * 64,
                oci_archive_sha256="2" * 64,
                state="revoked",
                created_at=NOW - timedelta(hours=3),
                updated_at=NOW - timedelta(hours=3),
            )
        )

    # Short by one byte: the idle image goes, the recently pulled one waits.
    result = _collector(
        world,
        image_cache_root=tmp_path,
        free=100 * GIB - 1,
        reserve_floor_bytes=0,
        reserve_fraction=0.0,
    ).collect()

    assert named.exists() and pulled.exists()
    assert not idle.exists()
    assert _kept(result, "live operation") == 1

    result = _collector(world, image_cache_root=tmp_path).collect()
    assert named.exists() and not pulled.exists()


@pytest.mark.usefixtures("damaged_json_rows")
def test_receipt_an_installation_still_uses_is_kept(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches removing the image of an installed (even idle) workload."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    installation, _ = world.workload(old, run="stopped")
    with world.sessions.begin() as session:
        row = session.get(RecipeInstallation, installation)
        assert row is not None
        row.plan = {"runtime_storage": {"oci_layout_sha256": AN_IMAGE}}
    path = _receipt(tmp_path, AN_IMAGE)

    _collector(world, image_cache_root=tmp_path).collect()

    assert path.exists()


@pytest.mark.usefixtures("damaged_json_rows")
def test_receipt_a_load_references_after_the_sweep_looked_is_kept(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches removal that races a load: re-proven under the gate and lock."""

    path = _receipt(tmp_path, AN_IMAGE)
    collector = _collector(world, image_cache_root=tmp_path)
    storage = collector._images
    assert storage is not None
    real_lock = storage.publication_lock

    @contextmanager
    def lock_then_load(archive: str):
        with real_lock(archive):
            world.job({"image": archive}, state="running", updated=NOW)
            yield

    storage.publication_lock = lock_then_load  # type: ignore[method-assign]

    result = collector.collect()

    assert path.exists()
    assert result.images == 0
    assert _kept(result, "live operation") == 1


def test_receipt_another_removal_owns_is_left_alone(
    world: Catalog, tmp_path: Path
) -> None:
    path = _receipt(tmp_path, AN_IMAGE)
    with world.sessions.begin() as session:
        session.add(
            ArtifactLifecycleGate(
                artifact_kind="runtime-image",
                artifact_sha256=AN_IMAGE,
                removal_owner_kind="recipe-image-job",
                removal_owner_id=str(uuid.uuid4()),
                removal_fence=str(uuid.uuid4()),
                updated_at=NOW,
            )
        )

    result = _collector(world, image_cache_root=tmp_path).collect()

    assert path.exists()
    assert _kept(result, "another removal") == 1


# -- cached models ----------------------------------------------------------------


def _head_recipe_naming(world: Catalog, model_digest: str) -> str:
    return world.revision(
        "glm",
        1,
        head="active",
        document={"models": [{"model": {"content_sha256": model_digest}}]},
    )


@pytest.fixture
def cached(tmp_path: Path, world: Catalog):
    """A real model cache over the same database, holding one model."""

    service = ModelCacheService(
        world.sessions, tmp_path / "nas-cache", reserve_bytes=0, fixture_sources=True
    )
    downloaded = _download(
        service,
        [_artifact(tmp_path, b"weights", model_content_sha256=A_MODEL)],
        model_content_sha256=A_MODEL,
        request_key=str(uuid.uuid4()),
    )
    assert downloaded.state == "succeeded"
    yield service, str(downloaded.artifact_set_sha256)
    service.close()


def _models(world: Catalog, service: ModelCacheService, **kwargs):
    return _collector(
        world,
        model_cache=service,
        model_cache_root=service._root,
        now=datetime.now(UTC) + GRACE + timedelta(hours=1),
        **kwargs,
    )


def _settle(service: ModelCacheService) -> None:
    for _ in range(16):
        service.advance_removals(limit=4)


def _set_exists(world: Catalog, set_digest: str) -> bool:
    with world.sessions() as session:
        return session.get(ModelCacheSet, set_digest) is not None


def test_model_no_recipe_needs_is_removed_when_disk_is_short_through_the_fenced_removal(
    world: Catalog, cached
) -> None:
    """Catches cached weights outliving every profile that named them, and a
    second removal queued while the first is still freeing its files."""

    service, set_digest = cached
    _head_recipe_naming(world, "b" * 64)  # a current recipe, for another model

    collector = _models(world, service)
    result = collector.collect()

    assert result.models == 1, result.kept
    with world.sessions() as session:
        removal = session.scalar(
            select(ModelCacheOperation).where(ModelCacheOperation.kind == "remove")
        )
        assert removal is not None and removal.actor == ACTOR
    # Until the cache has deleted the files, free space says nothing new.
    waiting = collector.collect()
    assert waiting.models == 0
    assert _scope(waiting, "nas")["outcome"] == "waiting"
    _settle(service)
    assert not _set_exists(world, set_digest)


def test_model_is_kept_while_disk_is_not_short(world: Catalog, cached) -> None:
    """Catches removing an idle model nobody is waiting for room for."""

    service, set_digest = cached

    result = _models(world, service, free=TOTAL // 2).collect()

    assert result.models == 0
    assert _set_exists(world, set_digest)


def test_a_download_refused_for_lack_of_disk_frees_the_models_it_can(
    world: Catalog, cached
) -> None:
    """Catches a refused download that never makes room, and one that removes
    models when even all of them could not make it fit."""

    service, set_digest = cached
    demands = StorageDemands(lambda: world.now)
    # 200 GiB free is above the low line, so only the refusal asks for more.
    collector = _models(world, service, free=200 * GIB, demands=demands)
    demands.request(
        NAS_MODELS,
        200 * GIB + 1024**2,
        source="model-download",
        reason="insufficient-reserved-storage",
    )

    short = collector.collect()
    assert short.models == 0
    assert _scope(short, "nas")["outcome"] == "insufficient_after_eviction"
    assert _set_exists(world, set_digest)

    demands.request(
        NAS_MODELS,
        200 * GIB + 3,
        source="model-download",
        reason="insufficient-reserved-storage",
    )
    covered = collector.collect()
    assert covered.models == 1
    assert str(_scope(covered, "nas")["why"]).startswith("refused: model-download")


def test_model_a_profile_loads_is_kept_and_a_merely_offered_one_is_not(
    world: Catalog, cached
) -> None:
    """Catches removing the weights of a profile's recipe, and keeping every
    model the catalog offers (the NAS would then never have room to free)."""

    service, set_digest = cached
    _head_recipe_naming(world, A_MODEL)

    offered = _models(world, service).collect()
    assert offered.models == 1, offered.kept
    _settle(service)
    assert not _set_exists(world, set_digest)


def test_model_a_profile_points_to_is_kept(world: Catalog, cached) -> None:
    service, set_digest = cached
    _head_recipe_naming(world, A_MODEL)
    _profile(world, "vonk-forge/glm")

    result = _models(world, service).collect()

    assert result.models == 0
    assert _kept(result, "profile") == 1
    assert _set_exists(world, set_digest)


@pytest.mark.usefixtures("damaged_json_rows")
def test_model_an_installation_or_a_live_operation_uses_is_kept(
    world: Catalog, cached
) -> None:
    """Catches removing weights a workload or an accepted load still names."""

    service, set_digest = cached
    old = world.revision("old", 1)
    world.revision("old", 2, head="active")
    installation, _ = world.workload(old, run="stopped")
    with world.sessions.begin() as session:
        row = session.get(RecipeInstallation, installation)
        assert row is not None
        row.plan = {"model": A_MODEL}

    result = _models(world, service).collect()

    assert result.models == 0
    assert _kept(result, "live operation") == 1
    assert _set_exists(world, set_digest)


def test_model_a_recent_transfer_to_a_spark_only_makes_it_the_last_to_go(
    world: Catalog, cached
) -> None:
    """A transfer in the last day is a reason to evict it last, not to keep it
    from a download that is waiting for room."""

    service, set_digest = cached
    stamp = datetime.now(UTC) + timedelta(hours=2)
    with world.sessions.begin() as session:
        session.add(
            ArtifactDistributionAssignment(
                plan_digest="3" * 64,
                node_id=NODE,
                generation=1,
                expires_at=stamp,
                model_artifact_set_sha256=set_digest,
                objects=[],
                oci_image_digest="sha256:" + "5" * 64,
                oci_image_config_digest="sha256:" + "6" * 64,
                oci_archive_sha256="2" * 64,
                state="revoked",
                created_at=stamp,
                updated_at=stamp,
            )
        )

    result = _models(world, service).collect()

    assert result.models == 1


@pytest.mark.usefixtures("damaged_json_rows")
def test_model_a_load_reaches_after_the_sweep_looked_is_not_fenced(
    world: Catalog, cached
) -> None:
    """Catches a fence left behind for a model a load has just reached: the
    removal is re-proven with every gate held and rolled back when refused."""

    service, set_digest = cached
    real = service.accept_unused_removal

    def load_arrives_first(digest: str, **kwargs):
        world.job(
            {"plan": {"model": digest}}, state="running", updated=datetime.now(UTC)
        )
        return real(digest, **kwargs)

    service.accept_unused_removal = load_arrives_first  # type: ignore[method-assign]

    result = _models(world, service).collect()

    assert result.models == 0
    assert _kept(result, "live operation") == 1
    with world.sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ModelCacheOperation)
                .where(ModelCacheOperation.kind == "remove")
            )
            == 0
        )
        assert not [
            gate
            for gate in session.scalars(select(ArtifactLifecycleGate))
            if gate.removal_owner_id is not None
        ]
    assert _set_exists(world, set_digest)


def test_one_pass_logs_one_line_with_what_was_freed_why_and_what_was_kept(
    world: Catalog, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Operators read the why, the bytes and the reasons kept from one line."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.workload(old, run="running")
    _receipt(tmp_path, AN_IMAGE)
    caplog.set_level("INFO")

    collector = _collector(world, image_cache_root=tmp_path)
    collector.collect()
    collector.collect()

    lines = [
        record.getMessage()
        for record in caplog.records
        if "unused_storage.eviction_pass" in record.getMessage()
    ]
    # The second pass found nothing new to say, so it says nothing.
    assert len(lines) == 1
    assert "installation: running" in lines[0]
    assert "image_receipts_removed" in lines[0]
    assert "low-space line" in lines[0]
    assert "estimated_freed_bytes" in lines[0]


# -- work refused for lack of disk ------------------------------------------------


def test_work_refused_for_lack_of_disk_frees_exactly_what_it_asked_for(
    world: Catalog,
) -> None:
    """Catches a refused load that never makes room, and one that frees more
    than its shortfall plus the reserve."""

    oldest, middle, _newest = _three_idle(world, newest_age=timedelta(days=2))
    demands = StorageDemands(lambda: world.now)
    lifecycle = FakeLifecycle(world.sessions)
    # 400 GiB free is well above the low line: only the refused load asks.
    collector = _collector(world, lifecycle, free=400 * GIB, demands=demands)
    assert collector.collect().installations == 0

    demands.request(
        spark_scope(NODE),
        450 * GIB,
        source="profile-load",
        subject="p",
        reason="run-switch.insufficient-disk",
    )
    result = collector.collect()

    # 50 GiB short plus the 20 GiB reserve: the oldest 80 GiB covers it.
    assert lifecycle.removed == [oldest]
    assert middle not in lifecycle.removed
    scope = _scope(result, spark_scope(NODE))
    assert scope["needed_bytes"] == 50 * GIB
    assert scope["why"] == "refused: profile-load run-switch.insufficient-disk"


def test_a_request_everything_removable_could_not_meet_removes_nothing_and_says_so(
    world: Catalog,
) -> None:
    """Removing part of the cache for a load that still would not fit costs the
    cache and helps nobody; the waiting load is told what would be enough."""

    _three_idle(world, newest_age=timedelta(days=2))
    demands = StorageDemands(lambda: world.now)
    lifecycle = FakeLifecycle(world.sessions)
    collector = _collector(world, lifecycle, free=400 * GIB, demands=demands)

    relief = collector.relief_for_spark(
        NODE, 800 * GIB, source="profile-load", subject="p", reason="x"
    )
    result = collector.collect()

    assert relief is not None
    assert relief.code == STORAGE_INSUFFICIENT
    assert (relief.needed_bytes, relief.freeable_bytes) == (400 * GIB, 240 * GIB)
    assert str(400 * GIB) in relief.detail and str(240 * GIB) in relief.detail
    assert lifecycle.removed == []
    assert _scope(result, spark_scope(NODE))["outcome"] == "insufficient_after_eviction"


def test_a_request_that_removal_can_meet_is_reported_as_evicting(
    world: Catalog,
) -> None:
    _three_idle(world, newest_age=timedelta(days=2))
    collector = _collector(
        world, free=400 * GIB, demands=StorageDemands(lambda: world.now)
    )

    relief = collector.relief_for_spark(
        NODE, 450 * GIB, source="profile-load", subject="p", reason="x"
    )

    assert relief is not None and relief.code == STORAGE_EVICTING
    assert (relief.needed_bytes, relief.freeable_bytes) == (50 * GIB, 240 * GIB)


def test_no_relief_is_offered_when_the_free_space_is_unknown_or_enough(
    world: Catalog,
) -> None:
    collector = _collector(world, free=400 * GIB)

    assert (
        collector.relief_for_spark(
            NODE, 300 * GIB, source="profile-load", subject="p", reason="x"
        )
        is None
    )
    # A Spark that has reported nothing recently is not guessed at.
    world.now = NOW + timedelta(hours=1)
    assert (
        collector.relief_for_spark(
            NODE, 900 * GIB, source="profile-load", subject="p", reason="x"
        )
        is None
    )


def test_a_demand_lapses_unless_the_refused_work_asks_again() -> None:
    """Catches evicting for a load that was cancelled or went away."""

    now = [NOW]
    demands = StorageDemands(lambda: now[0], ttl=timedelta(minutes=10))
    demands.request(spark_scope(NODE), GIB, source="install", reason="x")
    assert len(demands.active()) == 1

    now[0] = NOW + timedelta(minutes=11)
    assert demands.active() == []

    demands.request(spark_scope(NODE), GIB, source="install", reason="x")
    demands.settle(spark_scope(NODE), 2 * GIB)
    assert demands.active() == []


# -- one round at a time ------------------------------------------------------------


def test_a_second_removal_waits_for_the_first_to_show_in_a_fresh_report(
    world: Catalog,
) -> None:
    """Hard links make the bytes a removal frees uncertain, so the Spark's own
    next report decides whether more must go (catches queueing more while the
    first is still running, or acting on a report older than its landing)."""

    _three_idle(world, newest_age=timedelta(days=2))
    lifecycle = FakeLifecycle(world.sessions)
    collector = _collector(world, lifecycle)
    _unattended_uninstall(world, state="running", updated=NOW)

    assert collector.collect().installations == 0

    # Landed, but the Spark has not reported since.
    with world.sessions.begin() as session:
        job = session.scalar(select(Job).where(Job.kind == "recipe.uninstall"))
        assert job is not None
        job.state, job.updated_at = "succeeded", NOW + timedelta(seconds=10)
    world.now = NOW + timedelta(seconds=20)
    _report_disk(world.sessions, GIB, at=NOW + timedelta(seconds=5))
    waiting = collector.collect()
    assert waiting.installations == 0
    assert _scope(waiting, spark_scope(NODE))["outcome"] == "waiting"

    # A report after it landed: the Spark is still short, so more goes.
    _report_disk(world.sessions, GIB, at=NOW + timedelta(seconds=15))
    assert collector.collect().installations >= 1


def test_removals_that_free_far_less_than_promised_pause_eviction(
    world: Catalog, caplog: pytest.LogCaptureFixture
) -> None:
    """Catches a cascade: removing installation after installation whose files
    another installation keeps linked, none of which frees anything."""

    oldest, middle, _newest = _three_idle(world, newest_age=timedelta(days=2))
    lifecycle = FakeLifecycle(world.sessions, apply=True)
    caplog.set_level("INFO")
    collector = _collector(world, lifecycle, free=90 * GIB)
    collector.collect()
    assert lifecycle.removed == [oldest]

    # The Spark reports the same free space after the removal: it freed nothing.
    world.now = NOW + timedelta(minutes=2)
    _report_disk(world.sessions, 90 * GIB, at=world.now)
    paused = collector.collect()

    assert lifecycle.removed == [oldest]
    assert _scope(paused, spark_scope(NODE))["outcome"] == "paused"
    assert any(
        "unused_storage.ineffective" in record.getMessage() for record in caplog.records
    )

    # After the pause it tries again.
    world.now = NOW + timedelta(minutes=30)
    _report_disk(world.sessions, 90 * GIB, at=world.now)
    collector.collect()
    assert lifecycle.removed == [oldest, middle]


def test_removals_that_free_what_they_promised_do_not_pause_eviction(
    world: Catalog,
) -> None:
    _oldest, _middle, _newest = _three_idle(world, newest_age=timedelta(days=2))
    lifecycle = FakeLifecycle(world.sessions, apply=True)
    collector = _collector(world, lifecycle, free=1 * GIB)
    first = collector.collect().installations
    assert first == 2

    world.now = NOW + timedelta(minutes=2)
    _report_disk(world.sessions, 150 * GIB, at=world.now)
    assert collector.collect().installations == 0
    assert len(lifecycle.removed) == 2


# -- what keeps an installation when disk is short -------------------------------


@pytest.mark.usefixtures("damaged_json_rows")
def test_an_operation_that_just_finished_makes_an_installation_recent_not_kept(
    world: Catalog,
) -> None:
    """A sweep loads one recipe after another; each one it finished with is
    recently used (removed last), not in use. Catches every installation of the
    last 24 hours being unremovable, so a refused load can never be made room."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    used, _ = world.workload(old, run="stopped", touched=NOW - timedelta(hours=2))
    _size(world, used, 80 * GIB)
    world.job(
        {"plan": {"installation_id": used}},
        state="succeeded",
        updated=NOW - timedelta(hours=1),
    )
    lifecycle = FakeLifecycle(world.sessions)

    _collector(world, lifecycle).collect()

    assert lifecycle.removed == [used]


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_load_waiting_for_this_disk_does_not_keep_the_installations_it_needs_gone(
    world: Catalog,
) -> None:
    """The load that asks for space waits as a queued application naming the
    Spark. Catches it keeping every installation there, so the space it waits for
    could never be freed."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    installation, _ = world.workload(old, run="stopped")
    _size(world, installation, 80 * GIB)
    _application(
        world,
        {"steps": [{"node_ids": [NODE]}]},
        state="queued",
        progress={
            "blockers": [
                {
                    "code": "run-switch.insufficient-disk",
                    "detail": "short",
                    "severity": "warning",
                    "node_ids": [NODE],
                }
            ]
        },
    )
    lifecycle = FakeLifecycle(world.sessions)

    _collector(world, lifecycle).collect()

    assert lifecycle.removed == [installation]


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_refusal_says_what_stays_and_why(world: Catalog) -> None:
    """Catches 'only N bytes can be removed' with no word on what is kept."""

    head = world.revision("glm", 1, head="active")
    pointed, _ = world.workload(head, run="stopped")
    _size(world, pointed, 80 * GIB)
    _profile(world, "vonk-forge/glm")
    _application(world, {}, state="succeeded", selected=True)
    collector = _collector(
        world, free=400 * GIB, demands=StorageDemands(lambda: world.now)
    )

    relief = collector.relief_for_spark(
        NODE, 800 * GIB, source="profile-load", subject="p", reason="x"
    )

    assert relief is not None and relief.code == STORAGE_INSUFFICIENT
    assert (
        f"{80 * GIB} bytes of installations stay because the loaded profile uses them"
        in relief.detail
    )


def test_a_review_counts_every_unused_installation_and_names_what_stays(
    world: Catalog,
) -> None:
    """The read-only capacity a review plans eviction from: every installed but
    not running installation counts, a running one and a profile's recipe do
    not, and nothing is requested or removed. Catches a review that refuses a
    load while hundreds of GiB of unused installations could be evicted."""

    old = world.revision("glm", 1)
    head = world.revision("glm", 2, head="active")
    world.revision("llama", 1, head="active")
    idle = []
    for age in range(2, 14):
        installation, _ = world.workload(
            old, run="stopped", touched=NOW - timedelta(days=age)
        )
        _size(world, installation, 20 * GIB, model=f"{age:064x}")
        idle.append(installation)
    running, _ = world.workload(old, run="running")
    _size(world, running, 50 * GIB, model="e" * 64)
    pointed, _ = world.workload(head, run="stopped")
    _size(world, pointed, 70 * GIB, model="f" * 64)
    _profile(world, "vonk-forge/glm")
    lifecycle = FakeLifecycle(world.sessions)

    with world.sessions() as session:
        capacity = spark_eviction_capacity(session, NODE, NOW)

    # The profile's own installation may go too, after the 12 unpointed ones.
    assert capacity.freeable == 12 * 20 * GIB + 70 * GIB
    assert str(50 * GIB) in capacity.kept
    assert capacity.plan(13 * 20 * GIB) == (
        f"evicting {12 * 20 * GIB} bytes from unused installations; "
        f"evicting {70 * GIB} bytes from saved profile Coding"
    )
    assert lifecycle.removed == []


def test_unidentified_model_set_is_evicted_through_exact_fenced_scope(
    world: Catalog, cached
) -> None:
    service, set_digest = cached
    with world.sessions.begin() as session:
        model_set = session.get(ModelCacheSet, set_digest)
        assert model_set is not None
        model_set.model_content_sha256 = None
    result = _models(world, service).collect()
    assert result.models == 1, result.kept
    _settle(service)
    assert not _set_exists(world, set_digest)


def test_zero_verified_counter_does_not_hide_present_model_bytes(
    world: Catalog, cached
) -> None:
    service, set_digest = cached
    with world.sessions.begin() as session:
        model_set = session.get(ModelCacheSet, set_digest)
        assert model_set is not None
        model_set.verified_bytes = 0
    result = _models(world, service).collect()
    assert result.models == 1, result.kept
    _settle(service)
    assert not _set_exists(world, set_digest)


def test_failed_removal_releases_gate_and_fresh_download_reuses_model(
    world: Catalog,
    cached,
    tmp_path: Path,
) -> None:
    service, set_digest = cached
    removal = service.accept_unused_removal(
        A_MODEL,
        actor=ACTOR,
        request_key=str(uuid.uuid4()),
        verify=lambda _session, _sets: None,
    )
    with world.sessions.begin() as session:
        owner = session.get(ModelCacheOperation, removal.id)
        assert owner is not None
        owner.state = "failed"
    # An in-flight filesystem step must finish before its gate can be released.
    with service._model_storage_lock(set_digest, model_set=True):
        service.reconcile_removal_gates()
        with world.sessions() as session:
            gate = session.get(
                ArtifactLifecycleGate,
                {"artifact_kind": "model-set", "artifact_sha256": set_digest},
            )
            assert gate is not None and gate.removal_owner_id == removal.id
    service.reconcile_removal_gates()
    downloaded = _download(
        service,
        [_artifact(tmp_path, b"weights", model_content_sha256=A_MODEL)],
        model_content_sha256=A_MODEL,
        request_key=str(uuid.uuid4()),
    )
    assert downloaded.state == "succeeded"
    assert downloaded.artifact_set_sha256 == set_digest
