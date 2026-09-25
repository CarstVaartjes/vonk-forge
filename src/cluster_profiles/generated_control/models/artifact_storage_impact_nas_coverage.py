from typing import Literal, cast

ArtifactStorageImpactNasCoverage = Literal['complete', 'partial', 'unknown']

ARTIFACT_STORAGE_IMPACT_NAS_COVERAGE_VALUES: set[ArtifactStorageImpactNasCoverage] = { 'complete', 'partial', 'unknown',  }

def check_artifact_storage_impact_nas_coverage(value: str) -> ArtifactStorageImpactNasCoverage:
    if value in ARTIFACT_STORAGE_IMPACT_NAS_COVERAGE_VALUES:
        return cast(ArtifactStorageImpactNasCoverage, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ARTIFACT_STORAGE_IMPACT_NAS_COVERAGE_VALUES!r}")
