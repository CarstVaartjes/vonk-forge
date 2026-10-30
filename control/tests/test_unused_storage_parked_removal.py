"""A model removal parked on a dependency must not switch off NAS eviction.

A removal waiting in backoff for an installation to release its model frees
nothing and may wait for ever.  Only a removal that is deleting bytes now makes
the collector wait for fresh free-space numbers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from vonk_agent_protocol import LifecycleState, UnknownOutcomeError
from vonk_control import model_cache_states
from vonk_control.artifact_lifecycle import (
    ArtifactLifecycleError,
    ArtifactLifecycleGate,
)
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    Base,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeInstallation,
)
from vonk_control.storage_demands import NAS_MODELS, StorageDemands
from vonk_control.unused_storage_collection.common import _Kept

from .removal_settle import advance_until_ended
from .test_catalog_revision_collection import Catalog
from .test_model_cache import _artifact, _download
from .test_unused_storage_collection import (
    ACTOR,
    GIB,
    GRACE,
    _collector,
    _scope,
)


@pytest.fixture
def world(tmp_path: Path) -> Catalog:
    engine = create_engine(f"sqlite:///{tmp_path / 'controller.sqlite'}")

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return Catalog(engine)


PARKED = "c" * 64
OLDER = "a" * 64
NEWER = "b" * 64


def _cache_model(service: ModelCacheService, root: Path, model: str, data: bytes):
    downloaded = _download(
        service,
        [_artifact(root, data, artifact_id="weights", model_content_sha256=model)],
        model_content_sha256=model,
        request_key=str(uuid.uuid4()),
    )
    assert downloaded.state == "succeeded"
    return str(downloaded.artifact_set_sha256)


def _touch(world: Catalog, set_digest: str, when: datetime) -> None:
    with world.sessions.begin() as session:
        row = session.get(ModelCacheSet, set_digest)
        assert row is not None
        row.last_accessed_at = row.updated_at = when


@pytest.fixture
def cache(tmp_path: Path, world: Catalog):
    service = ModelCacheService(
        world.sessions, tmp_path / "nas-cache", free_fraction=0, fixture_sources=True
    )
    yield service
    service.close()


@pytest.fixture(autouse=True)
def _loose_rows(damaged_json_rows: None) -> None:
    return None


def _parked_world(world: Catalog, service: ModelCacheService, root: Path):
    older = _cache_model(service, root, OLDER, b"o" * 4096)
    newer = _cache_model(service, root, NEWER, b"newer-weights")
    parked = _cache_model(service, root, PARKED, b"parked-weights")
    base = datetime.now(UTC) - timedelta(days=5)
    _touch(world, older, base)
    _touch(world, newer, base + timedelta(days=1))
    _touch(world, parked, base + timedelta(days=2))

    # An installation that never settled holds the third model.
    revision = world.revision("held", 1)
    world.revision("held", 2, head="active")
    holder, _ = world.workload(revision, installation="installing", run="stopped")
    with world.sessions.begin() as session:
        row = session.get(RecipeInstallation, holder)
        assert row is not None
        row.plan = {"model": PARKED}
        row.model_content_sha256 = PARKED

    operation = service.remove_model_selector(
        PARKED, actor="admin", request_key=str(uuid.uuid4())
    )
    for _ in range(3):
        service.advance_removals(limit=1)
    with world.sessions() as session:
        row = session.get(ModelCacheOperation, operation.id)
        assert row is not None
        assert row.state in model_cache_states.words(LifecycleState.BACKOFF), row.state
    return older, newer, parked, operation.id


def _collector_under_pressure(world: Catalog, service: ModelCacheService):
    """A download refused for 5 bytes more than the NAS has: one model covers it."""

    demands = StorageDemands(lambda: world.now)
    collector = _collector(
        world,
        model_cache=service,
        model_cache_root=service._root,
        now=datetime.now(UTC) + GRACE + timedelta(hours=1),
        free=200 * GIB,
        reserve_floor_bytes=0,
        reserve_fraction=0,
        demands=demands,
    )
    demands.request(
        NAS_MODELS,
        200 * GIB + 5,
        source="model-download",
        reason="insufficient-reserved",
    )
    return collector


def _exists(world: Catalog, digest: str) -> bool:
    with world.sessions() as session:
        return session.get(ModelCacheSet, digest) is not None


def test_a_removal_parked_in_backoff_does_not_stop_nas_eviction(
    world: Catalog,
    cache: ModelCacheService,
    tmp_path: Path,
) -> None:
    """Catches a removal waiting for ever on an orphan installation keeping the
    Controller from evicting any NAS model under space pressure."""

    older, newer, parked, parked_operation = _parked_world(world, cache, tmp_path)
    collector = _collector_under_pressure(world, cache)

    result = collector.collect()

    assert _scope(result, "nas")["outcome"] != "waiting"
    assert result.models == 1, result.kept
    with world.sessions() as session:
        removals = session.scalars(
            select(ModelCacheOperation).where(ModelCacheOperation.kind == "remove")
        ).all()
        ours = [row for row in removals if row.actor == ACTOR]
        assert len(ours) == 1
        # The parked removal is untouched and still waits.
        untouched = session.get(ModelCacheOperation, parked_operation)
        assert untouched is not None
        assert untouched.state in model_cache_states.words(LifecycleState.BACKOFF)
    assert _exists(world, parked)
    advance_until_ended(cache, ours[0].id)
    assert not _exists(world, older)
    assert _exists(world, newer)
    assert _exists(world, parked)


def test_a_running_removal_still_makes_the_collector_wait(
    world: Catalog,
    cache: ModelCacheService,
    tmp_path: Path,
) -> None:
    """A removal deleting bytes now means free space says nothing new yet."""

    _parked_world(world, cache, tmp_path)
    with world.sessions.begin() as session:
        for row in session.scalars(
            select(ModelCacheOperation).where(ModelCacheOperation.kind == "remove")
        ):
            row.state = LifecycleState.RUNNING.value
    collector = _collector_under_pressure(world, cache)

    result = collector.collect()

    assert result.models == 0
    assert _scope(result, "nas")["outcome"] == "waiting"


@pytest.mark.parametrize("kind", ["model-set", "model-object"])
def test_unused_acceptance_refuses_what_another_removal_owns(
    world: Catalog,
    cache: ModelCacheService,
    tmp_path: Path,
    kind: str,
) -> None:
    """Catches the unattended acceptance fencing a set or object another removal
    already owns.  The refusal is one the pass moves on from (``_Kept``, an
    ``ArtifactLifecycleError`` or a retried unknown outcome), and the foreign owner keeps the gate."""

    older, _newer, _parked, _ = _parked_world(world, cache, tmp_path)
    with world.sessions() as session:
        digest = (
            older
            if kind == "model-set"
            else session.scalar(
                select(ModelCacheSetArtifact.artifact_sha256).where(
                    ModelCacheSetArtifact.artifact_set_sha256 == older
                )
            )
        )
    assert digest is not None
    foreign = str(uuid.uuid4())
    with world.sessions.begin() as session:
        gate = session.get(ArtifactLifecycleGate, (kind, digest))
        if gate is None:
            gate = ArtifactLifecycleGate(
                artifact_kind=kind, artifact_sha256=digest, updated_at=world.now
            )
            session.add(gate)
        gate.removal_owner_kind = "model-cache-operation"
        gate.removal_owner_id = foreign
        gate.removal_fence = str(uuid.uuid4())

    with pytest.raises((_Kept, ArtifactLifecycleError, UnknownOutcomeError)):
        cache.accept_unused_removal(
            OLDER,
            actor=ACTOR,
            request_key=str(uuid.uuid4()),
            verify=lambda _session, _sets: None,
        )

    with world.sessions() as session:
        gate = session.get(ArtifactLifecycleGate, (kind, digest))
        assert gate is not None and gate.removal_owner_id == foreign
        assert (
            session.scalar(
                select(ModelCacheOperation.id).where(
                    ModelCacheOperation.actor == ACTOR,
                    ModelCacheOperation.kind == "remove",
                )
            )
            is None
        )
