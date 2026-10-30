"""Callers see models: the cache answers how many bytes dropping sets would free."""

from datetime import UTC, datetime

from vonk_control.model_cache.inventory import InventoryMixin
from vonk_control.models import ModelCacheSet, ModelCacheSetArtifact
from vonk_control.run_switch_contract import (
    ArtifactStorageImpact,
    RunSwitchCleanupResult,
    RuntimeImageStorageImpact,
)

from .inventory_artifact_support import seed_inventory_artifact
from .test_recipe_operations import setup_services

NOW = datetime(2026, 10, 30, tzinfo=UTC)
SET_ONE, SET_TWO, UNKNOWN_SET = "1" * 64, "2" * 64, "3" * 64
SHARED, ONLY_TWO, IN_USE, OUTSIDE = "a" * 64, "b" * 64, "c" * 64, "d" * 64


def _set(digest: str) -> ModelCacheSet:
    return ModelCacheSet(
        artifact_set_sha256=digest,
        schema_version=2,
        model_content_sha256=digest,
        recipe_revision_sha256=None,
        manifest={},
        expected_bytes=1,
        verified_bytes=1,
        state="cached",
        protected=False,
        protected_reasons=[],
        created_at=NOW,
        updated_at=NOW,
        verified_at=NOW,
        last_accessed_at=NOW,
        last_error=None,
    )


def _member(set_digest: str, key: str, object_digest: str) -> ModelCacheSetArtifact:
    return ModelCacheSetArtifact(
        artifact_set_sha256=set_digest,
        artifact_key=key,
        artifact_sha256=object_digest,
        path=f"{key}.bin",
    )


def test_cache_answers_unused_bytes_of_sets_counting_shared_files_once(tmp_path):
    sessions, _lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    node = nodes[0]
    with sessions.begin() as session:
        session.add_all([_set(SET_ONE), _set(SET_TWO)])
        session.flush()
        session.add_all(
            [
                _member(SET_ONE, "shared", SHARED),
                _member(SET_ONE, "in-use", IN_USE),
                _member(SET_TWO, "shared", SHARED),
                _member(SET_TWO, "only-two", ONLY_TWO),
            ]
        )
        for digest, size, refs in (
            (SHARED, 10, 0),
            (ONLY_TWO, 20, 0),
            (IN_USE, 40, 1),
            (OUTSIDE, 80, 0),
        ):
            seed_inventory_artifact(
                session,
                node_id=node,
                kind="model",
                digest=digest,
                source="test",
                size_bytes=size,
                state="verified",
                ref_count=refs,
                updated_at=NOW.isoformat(),
            )
    cache = InventoryMixin()
    with sessions() as session:
        both = cache.reclaimable_bytes_for_sets(session, node, (SET_ONE, SET_TWO))
        one = cache.reclaimable_bytes_for_sets(session, node, (SET_ONE,))
        unknown = cache.reclaimable_bytes_for_sets(session, node, (UNKNOWN_SET,))
    assert both == 30  # shared once + only-two; in-use and outside files excluded
    assert one == 10
    assert unknown == 0


def test_cleanup_contracts_carry_bytes_only() -> None:
    for model in (ArtifactStorageImpact, RuntimeImageStorageImpact):
        assert "reclaimable_digests" not in model.model_fields
    for name in ("reclaimed_digests", "protected_digests"):
        assert name not in RunSwitchCleanupResult.model_fields
