"""Reference bookkeeping cannot interrupt unrelated accepted byte consumers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, current_thread
from uuid import uuid4

from sqlalchemy import Engine, event, select
from vonk_agent_protocol import LifecycleState
from vonk_agent_protocol.gpu_clock import GpuClockRange
from vonk_control.admission_locking import report_admission_locks
from vonk_control.distribution import (
    CompositeObjectSource,
    DistributionService,
    MemoryObjectSource,
    ModelCacheObjectSource,
)
from vonk_control.distribution_assignment import NodeDistributionAssignment
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.models import FleetProfile, ModelCacheSet

from .lifecycle_simulation.reach_world import (
    MODEL_SELECTOR,
    WEIGHTS,
    build_reach_world,
)
from .lifecycle_simulation.world import SOLO


def test_profile_saves_share_references_with_distribution_and_downloads(
    tmp_path: Path, postgres_engine: Engine
) -> None:
    """Catches whole-profile exclusive gates interrupting concurrent starts.

    Pause each real profile save immediately after its first gate acquisition.
    While the transaction stays open, a new exact distribution grant (the
    recipe.start manifest producer) and a real model download must finish.
    """
    world = build_reach_world(tmp_path, postgres_engine)
    engine = world.engine
    assert engine is not None
    cache = world.reach.cache
    held, release = Event(), Event()

    def pause_save(_conn, _cursor, statement, _parameters, _context, _many):
        if (
            current_thread().name.startswith("profile-save")
            and "pg_try_advisory_xact_lock" in statement
            and not held.is_set()
        ):
            held.set()
            assert release.wait(timeout=20)

    try:
        downloaded = cache.download_model_selector(
            MODEL_SELECTOR, actor="admin", request_key=str(uuid4())
        )
        cache.run_pending()
        assert cache.get_operation(downloaded.id).state == LifecycleState.SUCCEEDED
        with world.sessions() as session:
            set_digest = session.scalar(select(ModelCacheSet.artifact_set_sha256))
        assert set_digest is not None
        models = ModelCacheObjectSource.from_service(cache)
        objects = models.objects_for_set(set_digest)
        images = MemoryObjectSource()
        image = "sha256:" + "d" * 64
        archive = "e" * 64
        images.register_runtime_image(image, archive)
        delivery = DistributionService(
            CompositeObjectSource(models, images), sessions=world.sessions
        )
        event.listen(engine, "after_cursor_execute", pause_save)
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="profile-save"
        ) as pool:
            for index in range(4):
                held.clear()
                release.clear()
                desired_clock = GpuClockRange(min_mhz=300, max_mhz=1800 + index * 100)
                value = FleetProfileInput.model_validate(
                    {
                        "name": f"Concurrent pins {index}",
                        "assignments": [world.assignment(SOLO, world.nodes[:1])],
                    }
                )
                value.assignments[0].gpu_clock = desired_clock
                saving = pool.submit(
                    world.controller.profiles.create, value, actor="admin"
                )
                try:
                    assert held.wait(timeout=20)
                    assignment = NodeDistributionAssignment(
                        assignment_id=str(uuid4()),
                        plan_digest=f"{index + 1:064x}",
                        generation=1,
                        node_id=world.nodes[0],
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
                        model_artifact_set_sha256=set_digest,
                        objects=objects,
                        oci_image_digest=image,
                        oci_image_config_digest="sha256:" + "f" * 64,
                        oci_archive_sha256=archive,
                    )
                    delivery.register(assignment)
                    _, _, opened = delivery.open_object(
                        node_id=assignment.node_id,
                        plan_digest=assignment.plan_digest,
                        digest=objects[0].sha256,
                    )
                    with opened.stream:
                        assert opened.stream.read() == WEIGHTS
                    download = cache.download_model_selector(
                        MODEL_SELECTOR, actor="admin", request_key=str(uuid4())
                    )
                    cache.run_pending()
                    assert (
                        cache.get_operation(download.id).state
                        == LifecycleState.SUCCEEDED
                    )
                    assert not release.is_set()
                    with world.sessions() as session:
                        holders = report_admission_locks(session).held
                    assert holders and all(
                        holder.holder.startswith("profile-save:") for holder in holders
                    )
                finally:
                    release.set()
                    profile = saving.result(timeout=20)
                # The one normalization path must preserve the clock through
                # shared gate admission and the real JSON store/load boundary.
                with world.sessions() as session:
                    row = session.get(FleetProfile, profile.id)
                    assert row is not None
                    stored = world.controller.profiles._definition(row)
                assert stored is not None
                assert stored.assignments[0].gpu_clock == desired_clock
    finally:
        release.set()
        event.remove(engine, "after_cursor_execute", pause_save)
        world.close()


def test_unchanged_profile_pins_never_reacquire_reference_gates(
    tmp_path: Path, postgres_engine: Engine
) -> None:
    """Catches metadata/option edits and no-op replays re-claiming saved pins."""
    world = build_reach_world(tmp_path, postgres_engine)

    engine = world.engine
    assert engine is not None

    def reject_reference_change(
        _conn, _cursor, statement, _parameters, _context, _many
    ):
        assert "pg_try_advisory_xact_lock" not in statement

    try:
        value = FleetProfileInput.model_validate(
            {
                "name": "Retained pins",
                "assignments": [world.assignment(SOLO, world.nodes[:1])],
            }
        )
        profiles = world.controller.profiles
        profile = profiles.create(value, actor="admin")
        event.listen(engine, "before_cursor_execute", reject_reference_change)
        try:
            # Canonical saved choices include declared defaults.
            with world.sessions() as session:
                row = session.get(FleetProfile, profile.id)
                assert row is not None
                definition = profiles._definition(row)
            saved = FleetProfileInput.model_validate(
                {
                    **definition.model_dump(mode="json"),
                    "expected_revision": profile.revision,
                }
            )
            assert (
                profiles.update(profile.id, saved, actor="admin").revision
                == profile.revision
            )
            edited = saved.model_copy(update={"favorite": not saved.favorite})
            updated = profiles.update(profile.id, edited, actor="admin")
            assert updated.revision == profile.revision + 1
            assert (
                profiles.update(profile.id, edited, actor="admin").revision
                == updated.revision
            )
            clock_edit = edited.model_copy(deep=True)
            desired_clock = GpuClockRange(min_mhz=300, max_mhz=1700)
            clock_edit.assignments[0].gpu_clock = desired_clock
            clock_edit.expected_revision = updated.revision
            changed = profiles.update(profile.id, clock_edit, actor="admin")
            assert changed.revision == updated.revision + 1
            with world.sessions() as session:
                row = session.get(FleetProfile, profile.id)
                assert row is not None
                stored = profiles._definition(row)
            assert stored is not None
            assert stored.assignments[0].gpu_clock == desired_clock
        finally:
            event.remove(engine, "before_cursor_execute", reject_reference_change)
    finally:
        world.close()
