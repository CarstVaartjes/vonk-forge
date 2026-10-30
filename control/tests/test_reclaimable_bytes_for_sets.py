"""Callers see models: the cache answers how many bytes dropping sets would free."""

from datetime import UTC, datetime

from vonk_control.inventory_items import inventory_artifacts
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
        rows = [inventory_artifacts(session, node)]
        both = cache.reclaimable_bytes_for_sets(session, (SET_ONE, SET_TWO), rows)
        one = cache.reclaimable_bytes_for_sets(session, (SET_ONE,), rows)
        unknown = cache.reclaimable_bytes_for_sets(session, (UNKNOWN_SET,), rows)
    assert both == 30  # shared once + only-two; in-use and outside files excluded
    assert one == 10
    assert unknown == 0


def test_cleanup_plans_and_receipts_carry_bytes_and_read_old_lists() -> None:
    """New plans list no files; a stored plan or receipt with long lists still reads."""
    old = ["a" * 64] * 256  # stored data never exceeded the old cap
    assert (
        len(
            ArtifactStorageImpact.model_validate(
                _storage(reclaimable_digests=old)
            ).reclaimable_digests
        )
        == 256
    )
    assert ArtifactStorageImpact.model_validate(_storage()).reclaimable_digests == []
    assert RuntimeImageStorageImpact.model_fields["reclaimable_digests"].default_factory
    for name in ("reclaimed_digests", "protected_digests"):
        assert RunSwitchCleanupResult.model_fields[name].default_factory
    assert (
        len(
            RunSwitchCleanupResult.model_validate(
                {
                    "phase": "cleanup",
                    "scope": "spark-local",
                    "reclaimed_bytes": 1,
                    "nas_evicted": False,
                    "reclaimed_digests": old,
                }
            ).reclaimed_digests
        )
        == 256
    )


def _storage(**extra: object) -> dict[str, object]:
    return {
        "nas_coverage": "complete",
        "spark_coverage": "complete",
        "retention": "retain-cached",
        **extra,
    }


def test_a_set_of_more_than_a_thousand_files_is_a_valid_manifest() -> None:
    """The manifest byte limit bounds a set; a file count never did the right thing."""
    from vonk_control.model_cache import ArtifactSetManifest, ArtifactSpec
    from vonk_control.model_cache.artifacts import _validate_manifest

    model = "e" * 64
    manifest = ArtifactSetManifest(
        model_content_sha256=model,
        recipe_revision_sha256=None,
        model_content_digests=(model,),
        artifacts=tuple(
            ArtifactSpec(
                key=f"primary:shard-{index}",
                artifact_id=f"shard-{index}",
                path=f"shard-{index:05d}.safetensors",
                kind="huggingface",
                repository="vonk-forge/primary",
                source=f"https://huggingface.co/vonk-forge/primary/resolve/main/s{index}",
                revision="0" * 40,
                sha256=f"{index + 1:064x}",
                expected_bytes=1,
                roles=("weights",),
                model_content_sha256=model,
            )
            for index in range(1100)
        ),
    )
    _validate_manifest(manifest)


def _seed_many_file_model(sessions, node, *, files: int, held_by_nas: bool):
    """A Spark holding ``files`` unused verified files of one model, and its set."""
    from .test_model_set_storage_impact import FILES, _ManyFileModel

    provider = _ManyFileModel(missing_nas_bytes=0)
    manifest = provider.resolve_artifact_set(model_content_sha256="a" * 64)
    assert FILES >= files
    chosen = manifest.artifacts[:files]
    with sessions.begin() as session:
        if held_by_nas:
            session.add(_set(manifest.digest))
            session.flush()
            session.add_all(
                _member(manifest.digest, spec.key.replace(":", "-"), spec.sha256)
                for spec in chosen
            )
        for spec in chosen:
            seed_inventory_artifact(
                session,
                node_id=node,
                kind="model",
                digest=spec.sha256,
                source="test",
                size_bytes=spec.expected_bytes,
                state="verified",
                ref_count=0,
                updated_at=NOW.isoformat(),
            )
    return provider, manifest


def _inspect_with_reclaim(sessions, node, provider):
    from vonk_control.run_switch_operations import DatabaseRunSwitchArtifactInspector

    inspector = DatabaseRunSwitchArtifactInspector()
    inspector.bind_model_cache(provider)
    with sessions() as session:
        return inspector.inspect(
            session,
            model_content_sha256="a" * 64,
            recipe_revision_id="recipe-revision",
            node_ids=(node,),
            retention="reclaim-unreferenced",
            now=NOW,
        )


def test_a_286_file_model_plans_with_reclaim_through_the_real_inspector(tmp_path):
    """286 unused files on a Spark: reclaim plans as set + bytes (main: 256 cap)."""
    from vonk_control.run_switch_operations.phase_planning import PhasePlanningMixin

    sessions, _lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    provider, manifest = _seed_many_file_model(
        sessions, nodes[0], files=286, held_by_nas=True
    )
    inspection = _inspect_with_reclaim(sessions, nodes[0], provider)
    assert not inspection.blockers
    assert inspection.reclaimable_bytes == 286 * 1024
    impact = PhasePlanningMixin._storage(inspection, retention="reclaim-unreferenced")
    assert impact.reclaimable_bytes == 286 * 1024
    assert impact.reclaimable_digests == []
    assert impact.artifact_set_sha256 == manifest.digest


def test_a_set_the_nas_does_not_hold_frees_nothing(tmp_path):
    """No membership rows: the cache answers 0 and nothing is planned for deletion."""
    sessions, _lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    provider, _manifest = _seed_many_file_model(
        sessions, nodes[0], files=10, held_by_nas=False
    )
    inspection = _inspect_with_reclaim(sessions, nodes[0], provider)
    assert inspection.reclaimable_bytes == 0
