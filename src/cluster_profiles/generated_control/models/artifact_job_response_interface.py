from typing import Literal, cast

ArtifactJobResponseInterface = Literal['artifact-job', 'audio-job', 'image-job', 'mesh-job', 'video-job']

ARTIFACT_JOB_RESPONSE_INTERFACE_VALUES: set[ArtifactJobResponseInterface] = { 'artifact-job', 'audio-job', 'image-job', 'mesh-job', 'video-job',  }

def check_artifact_job_response_interface(value: str) -> ArtifactJobResponseInterface:
    if value in ARTIFACT_JOB_RESPONSE_INTERFACE_VALUES:
        return cast(ArtifactJobResponseInterface, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ARTIFACT_JOB_RESPONSE_INTERFACE_VALUES!r}")
