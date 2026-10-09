from __future__ import annotations

from datetime import UTC, datetime

from vonk_control.run_switch_operations import DatabaseRunSwitchArtifactInspector

from .test_run_switch_operations import ModelCacheManifestProvider, setup_services


def test_missing_artifact_authority_does_not_poison_repaired_inspection(
    tmp_path,
) -> None:
    inspector = DatabaseRunSwitchArtifactInspector()

    # No provider is a blocker the plan observes again, never an exception.
    inspection = inspector.inspect(
        None,  # type: ignore[arg-type]
        model_content_sha256="a" * 64,
        recipe_revision_id="recipe-revision",
        node_ids=("spk_" + "b" * 32,),
        retention="retain",
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )

    assert inspection.required_bytes is None
    assert inspection.missing_nas_bytes is None
    assert inspection.missing_spark_bytes is None
    assert not inspection.artifact_digests
    sessions, _lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    provider = ModelCacheManifestProvider(missing_nas_bytes=0)
    inspector.bind_model_cache(provider)
    with sessions() as session:
        repaired = inspector.inspect(
            session,
            model_content_sha256="a" * 64,
            recipe_revision_id="recipe-revision",
            node_ids=(nodes[0],),
            retention="retain",
            now=datetime(2026, 9, 6, tzinfo=UTC),
        )
    assert not repaired.blockers
    assert (
        repaired.artifact_set_sha256
        == provider.resolve_artifact_set(model_content_sha256="a" * 64).digest
    )
    assert repaired.missing_nas_bytes == 0
    assert repaired.missing_spark_bytes == repaired.artifact_set_bytes
    assert repaired.required_bytes == repaired.artifact_set_bytes
