from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vonk_control.run_switch_operations import DatabaseRunSwitchArtifactInspector


def test_run_switch_requires_the_controller_model_cache_manifest_provider() -> None:
    inspector = DatabaseRunSwitchArtifactInspector()

    with pytest.raises(RuntimeError, match="model-cache manifest provider"):
        inspector.inspect(
            None,  # type: ignore[arg-type]
            model_version_sha256="a" * 64,
            recipe_revision_id="recipe-revision",
            node_ids=("spk_" + "b" * 32,),
            retention="retain",
            now=datetime(2026, 9, 6, tzinfo=UTC),
        )

