"""A waiting load reaches the Spark collectors without manual cache removal."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from vonk_agent_protocol import InventoryVerdict, LifecycleState, StorageDemandCode
from vonk_control.fleet_profile_contract import FleetProfileApplicationProgress
from vonk_control.models import (
    Base,
    FleetProfileApplication,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeBuild,
)
from vonk_control.operation_blockers import OperationBlocker, StorageReliefEvidence
from vonk_control.spark_store_retention import store_retention_request

from .test_catalog_revision_collection import NODE, NOW, Catalog
from .test_unused_storage_collection import (
    A_MODEL,
    GIB,
    _application,
    _collector,
    _head_recipe_naming,
    _profile,
    _report_disk,
)
from .test_unused_storage_collection import (
    cached as cached_fixture,
)
from .test_unused_storage_collection import (
    world as world_fixture,
)

world = world_fixture
cached = cached_fixture


def _waiting_load(world: Catalog, *, required: int = 120 * GIB) -> str:
    progress = FleetProfileApplicationProgress(
        blockers=[
            OperationBlocker(
                code=StorageDemandCode.INSUFFICIENT_AFTER_EVICTION,
                detail="Waiting for disk",
                severity="warning",
                node_ids=[NODE],
                storage=StorageReliefEvidence(
                    required_free_bytes=required,
                    available_bytes=required * 3 // 4,
                    reclaimable_bytes=0,
                ),
            )
        ]
    )
    application_id = _application(
        world,
        {},
        state=LifecycleState.RUNNING,
        canonical=True,
        progress=progress.model_dump(mode="json"),
    )
    with world.sessions.begin() as session:
        application = session.get(FleetProfileApplication, application_id)
        assert application is not None
        application.updated_at = world.now
    return application_id


def test_waiting_load_demand_survives_postgres_and_inventory_round_trip(
    postgres_engine,
) -> None:
    """Catches an installation-only collector that never asks the native LRU
    to free the model-store bytes and keeps telling the load to remove work.
    """
    Base.metadata.create_all(postgres_engine)
    world = Catalog(postgres_engine)
    _report_disk(world.sessions, 90 * GIB, at=NOW, unused_models=0)
    application_id = _waiting_load(world)
    with world.sessions() as session:
        request = store_retention_request(session, NODE, NOW)
    assert request is not None
    verdict = InventoryVerdict.model_validate_json(
        InventoryVerdict(
            untracked_installations=[], store_retention=request
        ).model_dump_json(exclude_none=True)
    )
    assert verdict.store_retention is not None
    assert verdict.store_retention.required_free_bytes == 120 * GIB
    # The native collector's measured result arrives through the same inventory
    # storage consumed by load admission. Its operation can retry normally.
    _report_disk(world.sessions, 125 * GIB, at=NOW + timedelta(seconds=1))
    collector = _collector(world, free=125 * GIB)
    assert (
        collector.relief_for_spark(
            NODE,
            120 * GIB,
            source="profile-load",
            subject=application_id,
            reason="install",
        )
        is None
    )
    # A cancelled or stale load must not leave an everlasting eviction target.
    with world.sessions.begin() as session:
        application = session.get(FleetProfileApplication, application_id)
        assert application is not None
        application.state = LifecycleState.CANCELLED
    with world.sessions() as session:
        settled = store_retention_request(session, NODE, NOW)
    assert settled is not None and settled.required_free_bytes == 0


@pytest.mark.parametrize("owner", ("profile", "operation"))
def test_referenced_model_expands_to_protected_content_objects(
    world, cached, owner: str
) -> None:
    """A model digest is not its files' digest: protect actual content before
    an installation's plan exists on the Spark.
    """
    from vonk_control.model_cache_contract import CacheManifest

    _service, set_digest = cached
    with world.sessions() as session:
        row = session.get(ModelCacheSet, set_digest)
        assert row is not None
        manifest = CacheManifest.model_validate_json(json.dumps(row.manifest))
        expected = {artifact.sha256 for artifact in manifest.artifacts}
    if owner == "profile":
        _head_recipe_naming(world, A_MODEL)
        _profile(world, "vonk-forge/glm")
    else:
        with world.sessions.begin() as session:
            operation = session.scalar(select(ModelCacheOperation))
            assert operation is not None
            operation.state = LifecycleState.RUNNING
    with world.sessions() as session:
        request = store_retention_request(session, NODE, NOW)
    assert request is not None
    assert expected <= set(request.protected_model_objects)


def test_profile_build_layout_protects_its_docker_manifest_identity(world) -> None:
    """A layout's digest differs from Docker's manifest. Passing only the layout
    to native retention would remove a profile's source-built image.
    """
    revision = world.revision("glm", 1, head="active")
    build_id = world.build(revision, archive="d" * 64)
    _profile(world, "vonk-forge/glm")
    with world.sessions() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None and build.image_digest is not None
        request = store_retention_request(session, NODE, world.now)
        assert request is not None
        assert (
            build.image_digest.removeprefix("sha256:")
            in request.protected_image_addresses
        )
