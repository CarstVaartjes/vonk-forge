from typing import Literal, cast

ArtifactJobResponseState = Literal['cancelled', 'cancelling', 'draft', 'failed', 'queued', 'ready', 'running', 'succeeded', 'waiting-for-operator']

ARTIFACT_JOB_RESPONSE_STATE_VALUES: set[ArtifactJobResponseState] = { 'cancelled', 'cancelling', 'draft', 'failed', 'queued', 'ready', 'running', 'succeeded', 'waiting-for-operator',  }

def check_artifact_job_response_state(value: str) -> ArtifactJobResponseState:
    if value in ARTIFACT_JOB_RESPONSE_STATE_VALUES:
        return cast(ArtifactJobResponseState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ARTIFACT_JOB_RESPONSE_STATE_VALUES!r}")
