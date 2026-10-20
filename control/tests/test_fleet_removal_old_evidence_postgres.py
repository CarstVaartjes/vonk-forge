"""Partial persisted evidence stays visible and heals on the next review."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from vonk_agent_protocol import ArtifactLifecycleCode
from vonk_agent_protocol.inventory import ModelStoreObject
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.cache_removal_review import CacheRemovalReview
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    AgentNode,
    Base,
    ModelCacheSet,
    NodeInventorySnapshot,
    User,
)
from vonk_forge_contracts import RecipeDefinition

from .test_catalog_revision_collection import NODE, Catalog
from .test_fleet_model_removal_postgres import inventory
from .test_model_removal_reference_lifecycle import _one_model, _register_model
from .test_model_removal_worker_postgres import _prepare_removal_target
from .test_operator_acceptance_postgres import _api
from .test_recipe_operations import canonical_example


@pytest.mark.parametrize("fault", ["null", "inventory", "manifest"])
def test_review_of_shared_content_survives_partial_evidence_and_heals(
    postgres_engine, tmp_path: Path, fault: str
) -> None:
    """Catches unguarded persisted parsers and unknown reported as zero/empty.

    Two model definitions share bytes (as sibling recipe consumers do), both
    Sparks have historical NULL snapshots, and only one has current evidence.
    A damaged current row must not hide the healthy Spark or delete shared bytes.
    """
    Base.metadata.create_all(postgres_engine)
    world = Catalog(postgres_engine)
    second = "spk_" + "2" * 32
    with world.sessions.begin() as session:
        session.add(AgentNode(node_id=second, state=NodeIdentityState.ACTIVE))
        session.add(User(subject="operator", role="operator"))
    now = [datetime.now(UTC)]
    cache = ModelCacheService(
        world.sessions,
        tmp_path / "nas",
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now[0],
    )
    try:
        selector, model_digest, path = _prepare_removal_target(
            cache, world.sessions, tmp_path, slug="shared", data=b"shared weights"
        )
        for slug in ("sglang", "vllm"):
            document = canonical_example("recipe-source-build.json")
            document["identity"]["slug"] = slug
            document["models"][0]["model"].update(
                slug="shared", content_sha256=model_digest
            )
            recipe = RecipeDefinition.model_validate(document)
            world.revision(slug, 1, document=recipe.model_dump(mode="json"))
        _register_model(
            world.sessions, _one_model(tmp_path, "sibling", b"shared weights")
        )
        obj = ModelStoreObject(sha256=path.name, bytes=path.stat().st_size)
        for node in (NODE, second):
            inventory(world.sessions, node, now[0] - timedelta(minutes=1), None)
        inventory(world.sessions, second, now[0], [obj])
        manifest = None
        with world.sessions.begin() as session:
            if fault == "inventory":
                session.execute(
                    update(NodeInventorySnapshot)
                    .where(NodeInventorySnapshot.node_id == NODE)
                    .values(model_objects=[{"sha256": obj.sha256}])
                )
            elif fault == "manifest":
                row = session.scalar(select(ModelCacheSet))
                assert row is not None
                manifest = row.manifest
                row.manifest = {}
        client, headers = _api(world.sessions, model_cache=cache)
        with client:
            response = client.get(
                f"/api/model/{selector}/remove-review",
                params={"fleet": True},
                headers=headers,
            )
        assert response.status_code == 200, response.text
        wire = CacheRemovalReview.model_validate_json(response.content)
        assert wire.fleet is not None
        targets = {target.node_id: target for target in wire.fleet.targets}
        assert targets[NODE].observation_available is False
        assert targets[NODE].reclaimable_bytes is None
        assert targets[second].observation_available is True
        assert targets[second].reclaimable_bytes == 0
        assert targets[second].objects[0].object == obj
        assert wire.blockers and all(
            item.code == ArtifactLifecycleCode.REFERENCE_SCAN_FAILED
            for item in wire.blockers
        )
        assert path.read_bytes() == b"shared weights"
        # A read never repairs SQL or submits a removal. Fresh owner evidence heals.
        if manifest is not None:
            with world.sessions.begin() as session:
                row = session.scalar(select(ModelCacheSet))
                assert row is not None and row.manifest == {}
                row.manifest = manifest
        now[0] += timedelta(seconds=1)
        inventory(world.sessions, NODE, now[0], [obj])
        healed = cache.review_fleet_model_removal(selector)
        assert healed.fleet is not None
        assert all(target.observation_available for target in healed.fleet.targets)
        assert not healed.blockers
        assert path.read_bytes() == b"shared weights"
    finally:
        cache.close()


def test_unreadable_installation_is_unknown_and_next_review_recovers(
    postgres_engine, tmp_path: Path
) -> None:
    """Catches scanning an older installation plan before filtering its model."""
    from uuid import uuid4

    from vonk_control.lifecycle.evidence import BookkeepingReason
    from vonk_control.models import RecipeInstallation

    from .test_recipe_operations import NOW, installed_recipe, setup_services

    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine, nodes=2, mapping_node_count=1
    )
    installed = installed_recipe(
        lifecycle, mapping_id, build_id, nodes[:1], request_id=str(uuid4())
    )
    with sessions.begin() as session:
        row = session.get(RecipeInstallation, installed.owner_id)
        assert row is not None and row.model_content_sha256 is not None
        digest, original = row.model_content_sha256, row.plan
        row.plan = {}
    now = NOW + timedelta(seconds=1)
    cache = ModelCacheService(sessions, tmp_path / "nas", clock=lambda: now)
    cache._fleet_lifecycle = lifecycle
    try:
        for node in nodes:
            inventory(sessions, node, now, [])
        review = cache.review_fleet_model_removal(digest)
        assert review.fleet is not None and not review.fleet.nas_remove
        assert any(
            issue.subject == installed.owner_id
            and issue.reason == BookkeepingReason.PERSISTED_STATE_DAMAGED
            for issue in review.fleet.observation_issues
        )
        assert all(
            item.code == ArtifactLifecycleCode.REFERENCE_SCAN_FAILED
            for item in review.blockers
        )
        with sessions.begin() as session:
            row = session.get(RecipeInstallation, installed.owner_id)
            assert row is not None and row.plan == {}
            row.plan = original
        healed = cache.review_fleet_model_removal(digest)
        assert healed.fleet is not None and healed.fleet.nas_remove
        assert not healed.blockers
    finally:
        cache.close()
