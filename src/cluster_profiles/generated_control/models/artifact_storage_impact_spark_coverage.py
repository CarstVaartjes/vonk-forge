from typing import Literal

ArtifactStorageImpactSparkCoverage = Literal['complete', 'partial', 'unknown']

ARTIFACT_STORAGE_IMPACT_SPARK_COVERAGE_VALUES: set[ArtifactStorageImpactSparkCoverage] = { 'complete', 'partial', 'unknown',  }

def check_artifact_storage_impact_spark_coverage(value: str) -> ArtifactStorageImpactSparkCoverage:
    if value in ARTIFACT_STORAGE_IMPACT_SPARK_COVERAGE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ARTIFACT_STORAGE_IMPACT_SPARK_COVERAGE_VALUES!r}")
