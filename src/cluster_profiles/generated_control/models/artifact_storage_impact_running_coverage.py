from typing import Literal, cast

ArtifactStorageImpactRunningCoverage = Literal['complete', 'partial', 'unknown']

ARTIFACT_STORAGE_IMPACT_RUNNING_COVERAGE_VALUES: set[ArtifactStorageImpactRunningCoverage] = { 'complete', 'partial', 'unknown',  }

def check_artifact_storage_impact_running_coverage(value: str) -> ArtifactStorageImpactRunningCoverage:
    if value in ARTIFACT_STORAGE_IMPACT_RUNNING_COVERAGE_VALUES:
        return cast(ArtifactStorageImpactRunningCoverage, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ARTIFACT_STORAGE_IMPACT_RUNNING_COVERAGE_VALUES!r}")
