from typing import Literal

ArtifactJobResponsePreparationType0 = Literal['draft', 'ready']

ARTIFACT_JOB_RESPONSE_PREPARATION_TYPE_0_VALUES: set[ArtifactJobResponsePreparationType0] = { 'draft', 'ready',  }

def check_artifact_job_response_preparation_type_0(value: str) -> ArtifactJobResponsePreparationType0:
    if value in ARTIFACT_JOB_RESPONSE_PREPARATION_TYPE_0_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ARTIFACT_JOB_RESPONSE_PREPARATION_TYPE_0_VALUES!r}")
