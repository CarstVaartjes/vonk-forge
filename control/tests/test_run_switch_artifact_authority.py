from __future__ import annotations

from datetime import UTC, datetime

from vonk_control.run_switch_operations import DatabaseRunSwitchArtifactInspector


def test_run_switch_without_the_model_cache_provider_reports_unknown_coverage() -> None:
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

    assert inspection.nas_coverage == "unknown"
    assert inspection.spark_coverage == "unknown"
    assert [item.code for item in inspection.blockers] == [
        "run-switch.artifact-inspection-unavailable"
    ]
    assert "model-cache manifest provider" in inspection.blockers[0].detail
