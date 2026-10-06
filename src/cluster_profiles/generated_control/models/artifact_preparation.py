from typing import Literal

ArtifactPreparation = Literal['draft', 'ready']

ARTIFACT_PREPARATION_VALUES: set[ArtifactPreparation] = { 'draft', 'ready',  }

def check_artifact_preparation(value: str) -> ArtifactPreparation:
    if value in ARTIFACT_PREPARATION_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ARTIFACT_PREPARATION_VALUES!r}")
