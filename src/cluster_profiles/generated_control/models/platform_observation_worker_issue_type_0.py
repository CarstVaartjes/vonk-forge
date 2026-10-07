from typing import Literal

PlatformObservationWorkerIssueType0 = Literal['worker-observation-unavailable', 'worker-provenance-unavailable']

PLATFORM_OBSERVATION_WORKER_ISSUE_TYPE_0_VALUES: set[PlatformObservationWorkerIssueType0] = { 'worker-observation-unavailable', 'worker-provenance-unavailable',  }

def check_platform_observation_worker_issue_type_0(value: str) -> PlatformObservationWorkerIssueType0:
    if value in PLATFORM_OBSERVATION_WORKER_ISSUE_TYPE_0_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PLATFORM_OBSERVATION_WORKER_ISSUE_TYPE_0_VALUES!r}")
