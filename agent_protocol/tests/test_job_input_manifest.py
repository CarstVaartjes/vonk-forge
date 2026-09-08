"""Container adapters consume the same manifest as Controller job staging."""

import pytest
from pydantic import ValidationError
from vonk_agent_protocol.job_inputs import RecipeJobInputManifest
from vonk_agent_protocol.recipe_jobs import RecipeJobInputFile, manifest_document


def test_controller_input_manifest_is_the_adapter_contract() -> None:
    files = (
        RecipeJobInputFile(
            slot="prompt",
            name="prompt.txt",
            media_type="text/plain",
            size_bytes=12,
            sha256="a" * 64,
        ),
    )
    document = manifest_document(files)
    manifest = RecipeJobInputManifest.model_validate(document)
    assert manifest.files[0].slot == "prompt"
    assert manifest.model_dump(mode="json") == document


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"total_bytes": "12"},
        {"total_bytes": 13},
        {"files": []},
        {"unknown": "not a declared field"},
    ],
)
def test_adapter_manifest_rejects_invalid_structure(change: dict[str, object]) -> None:
    document = {
        "schema_version": 1,
        "total_bytes": 12,
        "files": [
            {
                "slot": "prompt",
                "name": "prompt.txt",
                "media_type": "text/plain",
                "size_bytes": 12,
                "sha256": "a" * 64,
            }
        ],
    }
    with pytest.raises(ValidationError):
        RecipeJobInputManifest.model_validate({**document, **change})
