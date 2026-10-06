from typing import Literal

ArtifactJobResponseStateType0 = Literal['backoff', 'cancelled', 'failed', 'needs-operator', 'observing', 'queued', 'running', 'succeeded']

ARTIFACT_JOB_RESPONSE_STATE_TYPE_0_VALUES: set[ArtifactJobResponseStateType0] = { 'backoff', 'cancelled', 'failed', 'needs-operator', 'observing', 'queued', 'running', 'succeeded',  }

def check_artifact_job_response_state_type_0(value: str) -> ArtifactJobResponseStateType0:
    if value in ARTIFACT_JOB_RESPONSE_STATE_TYPE_0_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ARTIFACT_JOB_RESPONSE_STATE_TYPE_0_VALUES!r}")
