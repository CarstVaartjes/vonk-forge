from typing import Literal

LifecycleSubject = Literal['AgentOperation', 'AgentOperationAttempt', 'ArtifactJob', 'FleetProfileApplication', 'Job', 'JobAttempt', 'ModelCacheOperation']

LIFECYCLE_SUBJECT_VALUES: set[LifecycleSubject] = { 'AgentOperation', 'AgentOperationAttempt', 'ArtifactJob', 'FleetProfileApplication', 'Job', 'JobAttempt', 'ModelCacheOperation',  }

def check_lifecycle_subject(value: str) -> LifecycleSubject:
    if value in LIFECYCLE_SUBJECT_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIFECYCLE_SUBJECT_VALUES!r}")
