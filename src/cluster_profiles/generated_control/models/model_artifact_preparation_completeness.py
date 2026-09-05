from typing import Literal, cast

ModelArtifactPreparationCompleteness = Literal['complete', 'incomplete', 'unknown']

MODEL_ARTIFACT_PREPARATION_COMPLETENESS_VALUES: set[ModelArtifactPreparationCompleteness] = { 'complete', 'incomplete', 'unknown',  }

def check_model_artifact_preparation_completeness(value: str) -> ModelArtifactPreparationCompleteness:
    if value in MODEL_ARTIFACT_PREPARATION_COMPLETENESS_VALUES:
        return cast(ModelArtifactPreparationCompleteness, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MODEL_ARTIFACT_PREPARATION_COMPLETENESS_VALUES!r}")
