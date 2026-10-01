"""Unused installations, image receipts and cached models are removed.

Each kind is removed after the grace period, and kept while a saved profile
points to it, a workload runs, it was used in the last day, or a live
operation names it. A load that starts after the sweep looked is never raced.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select
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
    RecipeInstallation,
    RecipeRun,
)
from vonk_control.recipe_operations import RecipeOperationConflict
from vonk_control.unused_storage_collection import (
    ACTOR,
    GRACE,
    UnusedStorageCollector,
)

from .test_catalog_revision_collection import NODE, NOW, OLD, Catalog
from .test_model_cache import _artifact, _download

A_MODEL = "a" * 64
AN_IMAGE = "d" * 64


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
    ) -> None:
        self._sessions = sessions
        self._between = between
        self._refuse = refuse
        self.removed: list[str] = []

    def preview_uninstall(self, installation_id: str):
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
    ) -> None:
        assert actor == ACTOR and unattended_guard is not None
        if installation_id in self._refuse:
            raise RecipeOperationConflict("refused")
        with self._sessions.begin() as session:
            unattended_guard(session)
        self.removed.append(installation_id)


def _collector(
    world: Catalog,
    lifecycle: FakeLifecycle | None = None,
    *,
    now: datetime | None = None,
    **options,
) -> UnusedStorageCollector:
    world.now = now or NOW
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


# -- installations ----------------------------------------------------------------


def test_installation_nothing_uses_is_uninstalled_and_a_superseded_one_goes_too(
    world: Catalog,
) -> None:
    """Catches a sweep that never uninstalls, or only the newest revision's."""

    old = world.revision("glm", 1)
    head = world.revision("glm", 2, head="active")
    stale, _ = world.workload(old, run="stopped")
    unpointed, _ = world.workload(head, run="stopped")
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert sorted(lifecycle.removed) == sorted([stale, unpointed])
    assert result.installations == 2


def test_installation_a_profile_points_to_is_kept_even_when_idle(
    world: Catalog,
) -> None:
    """Catches removing what a saved profile loads, loaded or not."""

    old = world.revision("glm", 1)
    head = world.revision("glm", 2, head="active")
    pointed, _ = world.workload(head, run="stopped")
    older, _ = world.workload(old, run="stopped")
    _profile(world, "vonk-forge/glm")
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    # Profiles follow the newest revision: the older installation is no one's.
    assert lifecycle.removed == [older]
    assert _kept(result, "profile") == 1
    assert pointed not in lifecycle.removed


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


def test_installation_used_in_the_last_day_or_superseded_in_it_is_kept(
    world: Catalog,
) -> None:
    """Catches removing before the 24 h grace, from use or from supersession."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    recent, _ = world.workload(old, run="stopped", touched=NOW - timedelta(hours=3))
    slow = world.revision("qwen", 1)
    world.revision("qwen", 2, created=NOW - timedelta(hours=2), head="active")
    superseded_lately, _ = world.workload(slow, run="stopped")
    lifecycle = FakeLifecycle(world.sessions)

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "recent use") == 2
    assert recent and superseded_lately

    # Once the grace period has passed with nothing touching them, both go.
    _collector(world, lifecycle, now=NOW + GRACE).collect()
    assert sorted(lifecycle.removed) == sorted([recent, superseded_lately])


def test_installation_a_recent_profile_edit_restarts_the_grace_period(
    world: Catalog,
) -> None:
    """A profile edit may have just dropped the assignment that used it."""

    head = world.revision("glm", 1, head="active")
    world.workload(head, run="stopped")
    _profile(world, "vonk-forge/other")
    with world.sessions.begin() as session:
        for profile in session.scalars(select(FleetProfile)):
            profile.updated_at = NOW - timedelta(hours=1)
    lifecycle = FakeLifecycle(world.sessions)

    _collector(world, lifecycle).collect()

    assert lifecycle.removed == []


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
    world: Catalog, plan: dict[str, object], *, state: str, selected: bool = False
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


def test_a_profile_saved_before_the_removal_is_queued_stops_the_removal(
    world: Catalog,
) -> None:
    """The guard re-reads the profiles, not only the sweep's first snapshot."""

    head = world.revision("glm", 1, head="active")
    world.workload(head, run="stopped")
    lifecycle = FakeLifecycle(
        world.sessions, between=lambda _id: _profile(world, "vonk-forge/glm")
    )

    result = _collector(world, lifecycle).collect()

    assert lifecycle.removed == []
    assert _kept(result, "profile") == 1


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


def _real_lifecycle(tmp_path: Path, *, nodes: int = 1):
    from .test_recipe_operations import installed_recipe, setup_services

    sessions, service, _queue, mapping_id, build_id, node_ids = setup_services(
        tmp_path, nodes=nodes
    )
    installation = installed_recipe(
        service, mapping_id, build_id, node_ids, request_id=str(uuid.uuid4())
    )
    return sessions, service, installation.owner_id, node_ids


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
    collector = UnusedStorageCollector(
        sessions,
        clock=lambda: datetime(2026, 8, 7, 12, tzinfo=UTC) + GRACE + timedelta(hours=1),
        lifecycle=service,
    )

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
    collector = UnusedStorageCollector(
        sessions,
        clock=lambda: datetime(2026, 8, 7, 12, tzinfo=UTC) + GRACE + timedelta(hours=1),
        lifecycle=service,
    )

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


# -- image receipts -----------------------------------------------------------------


def _receipt(root: Path, archive: str, *, age: timedelta = timedelta(days=3)) -> Path:
    path = root / "image-cache" / f"{archive}.receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_receipt_no_recipe_needs_is_removed_after_grace(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches receipts living forever once nothing names their image."""

    path = _receipt(tmp_path, AN_IMAGE)
    recent = _receipt(tmp_path, "e" * 64, age=timedelta(hours=2))

    result = _collector(world, image_cache_root=tmp_path).collect()

    assert not path.exists()
    assert recent.exists()
    assert result.images == 1
    assert _kept(result, "recent use") == 1
    with world.sessions() as session:
        gate = session.scalar(select(ArtifactLifecycleGate))
        assert gate is None or gate.removal_owner_id is None


def test_receipt_the_current_recipe_authorizes_is_kept(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches removing the image the newest revision would load."""

    head = world.revision("glm", 1, head="active")
    world.authorize(head, build_id=world.build(head), original_digest="0" * 64)
    path = _receipt(tmp_path, AN_IMAGE)

    result = _collector(world, image_cache_root=tmp_path).collect()

    assert path.exists()
    assert _kept(result, "current recipe") == 1


def test_receipt_of_a_superseded_revision_goes_but_a_shared_one_stays(
    world: Catalog, tmp_path: Path
) -> None:
    """The image an editorial successor reuses is the head's, so it stays."""

    old = world.revision("glm", 1)
    head = world.revision("glm", 2, head="active")
    world.authorize(old, build_id=world.build(old), original_digest="0" * 64)
    world.authorize(head, build_id=world.build(head), original_digest="1" * 64)
    shared = _receipt(tmp_path, AN_IMAGE)

    _collector(world, image_cache_root=tmp_path).collect()

    assert shared.exists()


def test_receipt_a_live_operation_names_or_a_recent_transfer_used_is_kept(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches removing an image a load or a Spark pull is using."""

    named = _receipt(tmp_path, "1" * 64)
    pulled = _receipt(tmp_path, "2" * 64)
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

    result = _collector(world, image_cache_root=tmp_path).collect()

    assert named.exists() and pulled.exists()
    assert _kept(result, "live operation") == 1
    assert _kept(result, "recent use") == 1


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
        now=datetime.now(UTC) + GRACE + timedelta(hours=1),
        **kwargs,
    )


def _settle(service: ModelCacheService) -> None:
    for _ in range(16):
        service.advance_removals(limit=4)


def _set_exists(world: Catalog, set_digest: str) -> bool:
    with world.sessions() as session:
        return session.get(ModelCacheSet, set_digest) is not None


def test_model_no_recipe_needs_is_removed_after_grace_through_the_fenced_removal(
    world: Catalog, cached
) -> None:
    """Catches cached weights outliving every recipe that named them."""

    service, set_digest = cached
    _head_recipe_naming(world, "b" * 64)  # a current recipe, for another model

    result = _models(world, service).collect()

    assert result.models == 1, result.kept
    with world.sessions() as session:
        removal = session.scalar(
            select(ModelCacheOperation).where(ModelCacheOperation.kind == "remove")
        )
        assert removal is not None and removal.actor == ACTOR
    _settle(service)
    assert not _set_exists(world, set_digest)


def test_model_in_the_cache_period_is_kept_until_the_grace_has_passed(
    world: Catalog, cached
) -> None:
    """Catches removing a freshly downloaded model: a load may be about to use it."""

    service, set_digest = cached

    result = _collector(world, model_cache=service, now=datetime.now(UTC)).collect()

    assert result.models == 0
    assert _kept(result, "recent use") == 1
    assert _set_exists(world, set_digest)


def test_model_a_current_recipe_names_is_kept_and_so_is_one_a_profile_loads(
    world: Catalog, cached
) -> None:
    """Catches removing the weights of the newest revision of a recipe."""

    service, set_digest = cached
    _head_recipe_naming(world, A_MODEL)
    _profile(world, "vonk-forge/glm")

    result = _models(world, service).collect()

    assert result.models == 0
    assert _kept(result, "current recipe") == 1
    assert _set_exists(world, set_digest)


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


def test_model_a_recent_transfer_to_a_spark_counts_as_use(
    world: Catalog, cached
) -> None:
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

    assert result.models == 0
    assert _kept(result, "recent use") == 1


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


def test_one_sweep_logs_one_summary_with_the_reasons_kept(
    world: Catalog, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Operators read removed and kept counts, and why, from one line."""

    old = world.revision("glm", 1)
    world.revision("glm", 2, head="active")
    world.workload(old, run="running")
    _receipt(tmp_path, AN_IMAGE)
    caplog.set_level("INFO")

    _collector(world, image_cache_root=tmp_path).collect()

    summaries = [
        record
        for record in caplog.records
        if "unused_storage.swept" in record.getMessage()
    ]
    assert len(summaries) == 1
    line = summaries[0].getMessage()
    assert "installation: running" in line
    assert "image_receipts_removed" in line
