"""Unused storage collection: kept models review."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_control.catalog.revision_collection import GRACE
from vonk_control.model_cache.service import ModelCacheService
from vonk_control.models.cache import (
    ArtifactLifecycleGate,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
)
from vonk_control.models.execution import (
    RecipeInstallation,
)
from vonk_control.models.fleet import (
    FleetProfile,
    FleetProfileApplication,
)
from vonk_control.models.jobs import Job
from vonk_control.models.operations import (
    ArtifactDistributionAssignment,
)
from vonk_control.recipe_operations.errors import RecipeOperationConflict
from vonk_control.storage_demands import (
    NAS_MODELS,
    STORAGE_EVICTING,
    STORAGE_INSUFFICIENT,
    StorageDemands,
    spark_scope,
)
from vonk_control.unused_storage_collection.common import ACTOR, spark_eviction_capacity
from vonk_forge_contracts.model import ModelReference

from .model_cache_support import _artifact, _download
from .test_catalog_revision_collection import NODE, NOW, Catalog
from .unused_storage_collection_support import (
    A_MODEL,
    AN_IMAGE,
    GIB,
    TOTAL,
    FakeLifecycle,
    _application,
    _collector,
    _head_recipe_naming,
    _kept,
    _models,
    _profile,
    _receipt,
    _report_disk,
    _scope,
    _set_exists,
    _settle,
    _size,
    _three_idle,
    _within_grace,
)


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


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("progress", [None, {"admission_pending": True}])
def test_receipt_a_waiting_load_resolved_is_kept_without_a_saved_profile(
    world: Catalog, tmp_path: Path, progress: dict[str, object] | None
) -> None:
    """A queued or admission-waiting application needs the image its plan named.

    The saved profile may name other recipes by now (a sweep rewrites it), and a
    load waiting for storage must still keep its own assets.
    """

    head = world.revision("glm", 1, head="active")
    world.build(head, archive=AN_IMAGE)
    path = _receipt(tmp_path, AN_IMAGE)
    plan: dict[str, object] = {"resolved_assignments": [{"recipe_revision_id": head}]}
    _application(world, plan, state="queued", progress=progress)

    result = _collector(world, image_cache_root=tmp_path).collect()

    assert path.exists()
    assert _kept(result, "profile application unreadable") == 1

    with world.sessions.begin() as session:
        for application in session.scalars(select(FleetProfileApplication)):
            session.delete(application)
        session.flush()
        for profile in session.scalars(select(FleetProfile)):
            session.delete(profile)
    result = _collector(world, image_cache_root=tmp_path).collect()
    assert not path.exists(), result.kept  # a fresh operation is not held back by it


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
    world.job({"plan": {"image": "1" * 64}}, state="observing", updated=NOW - GRACE)
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
    world.job({}, state="running", updated=NOW)
    with world.sessions.begin() as session:
        owner = session.scalar(select(Job.id))
        assert owner is not None
        session.add(
            ArtifactLifecycleGate(
                artifact_kind="runtime-image",
                artifact_sha256=AN_IMAGE,
                removal_owner_kind="recipe-image-job",
                removal_owner_id=owner,
                removal_fence=str(uuid.uuid4()),
                updated_at=NOW,
            )
        )

    result = _collector(world, image_cache_root=tmp_path).collect()

    assert path.exists()
    assert _kept(result, "another removal") == 1
    with world.sessions.begin() as session:
        ended = session.get(Job, owner)
        assert ended is not None
        ended.state = LifecycleState.FAILED
    assert _collector(world, image_cache_root=tmp_path).collect().images == 1
    assert not path.exists()
    fresh = _receipt(tmp_path, AN_IMAGE)
    assert _collector(world, image_cache_root=tmp_path).collect().images == 1
    assert not fresh.exists()


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
    # Until the cache has deleted the files, free space says nothing new: a
    # queued removal is not yet settling, but the round that freed nothing
    # pauses eviction.
    waiting = collector.collect()
    assert waiting.models == 0
    assert _scope(waiting, "nas")["outcome"] == "paused"
    _settle(service)
    assert not _set_exists(world, set_digest)


def _two_cached_sets(tmp_path: Path, service: ModelCacheService) -> tuple[str, str]:
    """Two cached sets that keep one identical file: A = shared + own, B = shared."""

    sets = []
    for model, files in (
        ("1" * 64, (("shared", b"shared-bytes"), ("own", b"a-own-bytes!!"))),
        ("2" * 64, (("shared", b"shared-bytes"),)),
    ):
        artifacts = [
            _artifact(
                tmp_path / model[:1],
                data,
                artifact_id=name,
                path=f"{name}.bin",
                model_content_sha256=model,
            )
            for name, data in files
        ]
        (tmp_path / model[:1]).mkdir(exist_ok=True)
        done = _download(
            service,
            artifacts,
            model_content_sha256=model,
            request_key=str(uuid.uuid4()),
        )
        assert done.state == "succeeded"
        sets.append(str(done.artifact_set_sha256))
    return sets[0], sets[1]


def test_unused_set_bytes_exclude_a_file_another_cached_set_keeps(
    tmp_path: Path, world: Catalog, cached
) -> None:
    """Catches counting a shared object as freed while another set still keeps it."""

    service, _first = cached
    (tmp_path / "1").mkdir()
    (tmp_path / "2").mkdir()
    a, b = _two_cached_sets(tmp_path, service)

    assert service.unused_set_bytes(a) == len(b"a-own-bytes!!")
    assert service.unused_set_bytes(b) == 0
    with world.sessions.begin() as session:
        for member in session.scalars(
            select(ModelCacheSetArtifact).where(
                ModelCacheSetArtifact.artifact_set_sha256 == b
            )
        ):
            session.delete(member)
        row = session.get(ModelCacheSet, b)
        assert row is not None
        session.delete(row)
    assert service.unused_set_bytes(a) == len(b"a-own-bytes!!") + len(b"shared-bytes")


def test_model_items_under_pressure_read_no_model_object(
    world: Catalog, cached, no_model_object_io
) -> None:
    """Catches the sweep measuring each set on the NAS (receipt, open, fstat, lstat)
    inside its database transaction: stored facts answer, storage is not touched."""

    service, _set_digest = cached
    _head_recipe_naming(world, "b" * 64)
    no_model_object_io(service)

    result = _models(world, service, disk_usage=lambda _p: (TOTAL, 0)).collect()

    assert result.models == 1, result.kept
    with world.sessions() as session:
        removal = session.scalar(
            select(ModelCacheOperation).where(ModelCacheOperation.kind == "remove")
        )
        assert removal is not None and removal.actor == ACTOR


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("pins_intact", [True, False])
def test_unused_set_bytes_unknown_when_the_cached_manifest_is_damaged(
    world: Catalog, cached, pins_intact: bool
) -> None:
    """Catches a guessed size for a set whose stored manifest is damaged, with
    the catalog pins still able to rebuild it or not."""

    service, set_digest = cached
    with world.sessions.begin() as session:
        row = session.get(ModelCacheSet, set_digest)
        assert row is not None
        row.manifest = {"schema_version": 2, "artifacts": "damaged"}
        if not pins_intact:
            row.model_content_sha256 = None
            row.recipe_revision_sha256 = None

    assert service.unused_set_bytes(set_digest) is None


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


def test_model_a_profile_points_to_under_a_new_digest_is_kept(
    world: Catalog, cached
) -> None:
    """Catches keeping a cached set only for the Model that first requested it.

    A release re-digests the Model (same files); the cache adopts the new
    digest as a verified binding and the profile points to that digest.
    """

    service, set_digest = cached
    redigested = "b" * 64
    with world.sessions.begin() as session:
        row = session.get(ModelCacheSet, set_digest)
        assert row is not None
        manifest = service._stored_manifest(row)
        assert manifest is not None
        reference = ModelReference(
            kind="model", publisher="vonk-forge", slug="glm", content_sha256=redigested
        )
        row.manifest = replace(manifest, verified_models=(reference,)).document()
    _head_recipe_naming(world, redigested)
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
    receipt = _receipt(tmp_path, AN_IMAGE)
    caplog.set_level("INFO")

    # The NAS disk shows the receipt's KiB freed once it is gone.
    collector = _collector(
        world,
        image_cache_root=tmp_path,
        disk_usage=lambda _path: (TOTAL, GIB + (0 if receipt.exists() else 1024)),
    )

    def lines() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if "unused_storage.eviction_pass" in record.getMessage()
        ]

    collector.collect()
    (line,) = lines()
    assert "installation: running" in line
    assert '"image_receipts_removed":1' in line
    assert "low-space line" in line
    assert "estimated_freed_bytes" in line
    collector.collect()  # sees the receipt gone
    seen = lines()
    collector.collect()
    # A pass that found nothing new to say says nothing.
    assert lines() == seen


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
    _within_grace(world)
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
    _within_grace(world)
    collector = _collector(world, lifecycle, free=400 * GIB, demands=demands)

    relief = collector.relief_for_spark(
        NODE, 800 * GIB, source="profile-load", subject="p", reason="x"
    )
    result = collector.collect()

    assert relief is not None
    assert (relief.needed_bytes, relief.freeable_bytes) == (400 * GIB, 240 * GIB)
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

    assert relief is not None
    assert (relief.needed_bytes, relief.freeable_bytes) == (50 * GIB, 240 * GIB)


@pytest.mark.parametrize("failure", ["refused", "deferred"])
def test_a_waiting_load_is_told_evicting_only_while_the_removal_can_happen(
    world: Catalog, monkeypatch, failure: str
) -> None:
    """Catches a load told ``storage.evicting`` for good while every removal it
    waits for is refused: the space it was promised never comes. A deferred
    removal (outcome unknown, retried next pass) is still under way; a refused
    one is not, until a later pass removes it after all."""
    from vonk_agent_protocol.lifecycle_vocabulary import WaitReason
    from vonk_control.recipe_operations.errors import RecipeRetryLater

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    stuck, _ = world.workload(old, run="stopped")
    _size(world, stuck, 80 * GIB)
    lifecycle = FakeLifecycle(world.sessions)
    uninstall = lifecycle.uninstall
    failing = [True]

    def refusing_uninstall(installation_id, **kwargs):
        if failure == "refused" and failing[0]:
            raise RecipeOperationConflict("refused")
        if failure == "deferred" and failing[0]:
            raise RecipeRetryLater(
                "unknown ownership", reason=WaitReason.OBSERVATION_UNAVAILABLE
            )
        return uninstall(installation_id, **kwargs)

    monkeypatch.setattr(lifecycle, "uninstall", refusing_uninstall)
    collector = _collector(
        world, lifecycle, free=400 * GIB, demands=StorageDemands(lambda: world.now)
    )

    def relief():
        found = collector.relief_for_spark(
            NODE, 450 * GIB, source="profile-load", subject="p", reason="x"
        )
        assert found is not None
        return found

    assert relief().code == STORAGE_EVICTING
    collector.collect()
    assert lifecycle.removed == []

    told = relief()
    if failure == "deferred":
        assert told.code == STORAGE_EVICTING
        assert told.freeable_bytes == 80 * GIB
    else:
        assert told.code == STORAGE_INSUFFICIENT
        assert told.freeable_bytes == 0
        assert "refused" in told.detail

    # It heals by itself: a later pass that can remove it does.
    failing[0] = False
    world.now += collector._scan_interval
    _report_disk(world.sessions, 400 * GIB, at=world.now)
    collector.collect()
    assert lifecycle.removed == [stuck]


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
    _within_grace(world)
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
    _within_grace(world)
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
    _within_grace(world)
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
        canonical=True,
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

    assert relief is not None


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


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("plan", [{}, {"resolved_assignments": "damaged"}])
def test_unreadable_pending_application_retains_unmentioned_image(
    world: Catalog, tmp_path: Path, plan: dict[str, object]
) -> None:
    """A failed reference read cannot prove an unrelated cached image unused."""
    head = world.revision("glm", 1, head="active")
    world.build(head, archive=AN_IMAGE)
    path = _receipt(tmp_path, AN_IMAGE)
    _application(world, plan, state="queued")
    result = _collector(world, image_cache_root=tmp_path).collect()
    assert path.exists()
    assert _kept(result, "profile application unreadable") == 1
    with world.sessions.begin() as session:
        for application in session.scalars(select(FleetProfileApplication)):
            session.delete(application)
        session.flush()
        for profile in session.scalars(select(FleetProfile)):
            session.delete(profile)
    result = _collector(world, image_cache_root=tmp_path).collect()
    assert not path.exists(), result.kept
