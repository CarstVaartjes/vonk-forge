import json
from pathlib import Path

import pytest
from vonk_control.artifact_sizes import ArtifactSizeError, DeclaredArtifactSizeResolver


def recipe():
    return json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )


def test_declared_recipe_sizes_resolve_immutable_external_artifacts() -> None:
    document = recipe()
    artifacts = DeclaredArtifactSizeResolver().resolve(document)

    assert artifacts[0].source == (
        "vonk-forge/synthetic-tiny@0123456789abcdef0123456789abcdef01234567"
    )
    assert artifacts[0].size_bytes == 1024
    assert len(artifacts[0].digest) == 64


def test_declared_sizes_reject_inconsistent_topology_artifact_totals() -> None:
    document = recipe()
    document["topology"]["roles"][0]["resources"]["disk"]["artifact_bytes"] = 1
    with pytest.raises(ArtifactSizeError, match="smaller"):
        DeclaredArtifactSizeResolver().resolve(document)
