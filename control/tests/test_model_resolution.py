import pytest
from vonk_control.model_resolution import (
    ModelFile,
    ModelResolutionError,
    SnapshotEnvelope,
    resolve_huggingface_snapshot,
)


class Models:
    def snapshot(self, repository: str, revision: str, *, maximum_files: int):
        return SnapshotEnvelope(
            repository=repository,
            revision=revision,
            files=(
                ModelFile("model-00001.safetensors", 100),
                ModelFile("tokenizer.json", 20),
            ),
        )


def test_model_resolution_records_bounded_logical_size_and_auxiliary_files() -> None:
    revision = "0123456789abcdef0123456789abcdef01234567"
    result = resolve_huggingface_snapshot("Qwen/Qwen3", revision, Models())
    assert result.expected_bytes == 120
    assert result.weight_files == ("model-00001.safetensors",)
    assert result.auxiliary_files == ("tokenizer.json",)


@pytest.mark.parametrize("revision", ["main", "latest", "refs/pr/12"])
def test_model_resolution_rejects_mutable_revisions(revision: str) -> None:
    with pytest.raises(ModelResolutionError) as caught:
        resolve_huggingface_snapshot("Qwen/Qwen3", revision, Models())
    assert caught.value.code == "model.mutable_revision"
