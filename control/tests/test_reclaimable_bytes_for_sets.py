"""Cleanup plans carry model bytes, not file lists."""

from vonk_control.run_switch_contract import (
    ArtifactStorageImpact,
    RunSwitchCleanupResult,
    RuntimeImageStorageImpact,
)


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
