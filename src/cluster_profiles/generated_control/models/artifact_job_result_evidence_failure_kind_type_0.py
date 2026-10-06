from typing import Literal

ArtifactJobResultEvidenceFailureKindType0 = Literal['agent-lease-expired', 'cancellation-stop-uncertain']

ARTIFACT_JOB_RESULT_EVIDENCE_FAILURE_KIND_TYPE_0_VALUES: set[ArtifactJobResultEvidenceFailureKindType0] = { 'agent-lease-expired', 'cancellation-stop-uncertain',  }

def check_artifact_job_result_evidence_failure_kind_type_0(value: str) -> ArtifactJobResultEvidenceFailureKindType0:
    if value in ARTIFACT_JOB_RESULT_EVIDENCE_FAILURE_KIND_TYPE_0_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ARTIFACT_JOB_RESULT_EVIDENCE_FAILURE_KIND_TYPE_0_VALUES!r}")
