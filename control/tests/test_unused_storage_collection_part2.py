from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    RunState,
)
from vonk_control.artifact_lifecycle import ArtifactLifecycleGate
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    AgentNode,
    ArtifactDistributionAssignment,
    Base,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
    InstallationNode,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeInstallation,
    RecipeRun,
    RunNode,
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
    spark_eviction_capacity,
)
from vonk_forge_contracts.model import ModelReference

from . import unused_storage_collection_support as _support
from .model_cache_support import _artifact, _download
from .removal_settle import release_ended_gates
from .test_catalog_revision_collection import NODE, NOW, Catalog
from .unused_storage_collection_support import (
    _REAL_CLOCK,
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
    _ordinals,
    _profile,
    _real_collector,
    _real_lifecycle,
    _receipt,
    _report_disk,
    _required,
    _scope,
    _set_exists,
    _settle,
    _size,
    _three_idle,
    _within_grace,
)

cached = _support.cached
world = _support.world


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
        assert installation is not None and installation.state == "installed"
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

    from vonk_control.distributed_recovery.common import (
        _settle_unrecoverable,
        release_inactive_run_claims_in_session,
    )

    from .recipe_operations_support import started_recipe

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

    from vonk_agent_protocol.capability_reply import ControllerCapability
    from vonk_control.capabilities import CapabilityRegistry

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
    from vonk_agent_protocol import WaitReason
    from vonk_control.recipe_operations import RecipeRetryLater

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
    # A complete bounded inventory cycle revisits the previously busy gate.
    release_ended_gates(service)
    downloaded = _download(
        service,
        [_artifact(tmp_path, b"weights", model_content_sha256=A_MODEL)],
        model_content_sha256=A_MODEL,
        request_key=str(uuid.uuid4()),
    )
    assert downloaded.state == "succeeded"
    assert downloaded.artifact_set_sha256 == set_digest


@pytest.mark.parametrize(
    "fault", ["none", "before-unlink", "after-unlink", "not-retired"]
)
def test_receipt_unlink_is_outside_sql_with_committed_fence_and_fresh_admission(
    postgres_engine: Engine, tmp_path: Path, monkeypatch, fault: str
) -> None:
    """Catches filesystem effects under SQL locks and gates leaked on failure."""
    from vonk_control.artifact_lifecycle import ArtifactIdentity, require_reference_open

    Base.metadata.create_all(postgres_engine)
    world = Catalog(postgres_engine)
    path = _receipt(tmp_path, AN_IMAGE)
    collector = _collector(world, image_cache_root=tmp_path)
    storage = collector._images
    assert storage is not None
    original = storage.remove_published
    attempts = []

    def unlink(archive):
        # A second transaction can take the gate NOWAIT, proving the unlink
        # caller holds no SQL locks. The committed fence still denies attachment.
        with world.sessions.begin() as session:
            gate = session.scalar(
                select(ArtifactLifecycleGate)
                .where(ArtifactLifecycleGate.artifact_sha256 == archive)
                .with_for_update(nowait=True)
            )
            assert gate is not None and gate.removal_owner_id is not None
            blocked = False
            try:
                require_reference_open(
                    session,
                    (ArtifactIdentity("runtime-image", archive),),
                    now=world.now,
                )
            except Exception:  # noqa: BLE001 - inspect actual attachment effect
                blocked = True
            assert blocked
        attempts.append(archive)
        if fault == "before-unlink" and len(attempts) == 1:
            raise OSError("storage temporarily unavailable")
        if fault == "not-retired" and len(attempts) == 1:
            return 0
        result = original(archive)
        if fault == "after-unlink" and len(attempts) == 1:
            raise OSError("unlink reply lost")
        return result

    monkeypatch.setattr(storage, "remove_published", unlink)
    collector.collect()
    with world.sessions.begin() as session:
        require_reference_open(
            session, (ArtifactIdentity("runtime-image", AN_IMAGE),), now=world.now
        )
    if path.exists():
        # Ended sweep owns no gate; a fresh sweep observes bytes and completes.
        fresh = _collector(world, image_cache_root=tmp_path)
        from vonk_control.persisted_waits import read_wait

        with world.sessions() as session:
            gate = session.get(
                ArtifactLifecycleGate,
                {
                    "artifact_kind": "runtime-image",
                    "artifact_sha256": AN_IMAGE,
                },
            )
            wait = read_wait(gate) if gate is not None else None
        if wait is not None:
            assert fresh.collect().images == 0  # restart retains the bounded retry
            world.now = wait.next_check
        assert fresh.collect().images == 1
    assert not path.exists()
    assert attempts == [AN_IMAGE]
    _receipt(tmp_path, AN_IMAGE)
    assert _collector(world, image_cache_root=tmp_path).collect().images == 1
