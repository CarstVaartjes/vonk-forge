from typing import Literal, cast

ArtifactStorageImpactRetention = Literal['reclaim-unreferenced', 'retain-cached']

ARTIFACT_STORAGE_IMPACT_RETENTION_VALUES: set[ArtifactStorageImpactRetention] = { 'reclaim-unreferenced', 'retain-cached',  }

def check_artifact_storage_impact_retention(value: str) -> ArtifactStorageImpactRetention:
    if value in ARTIFACT_STORAGE_IMPACT_RETENTION_VALUES:
        return cast(ArtifactStorageImpactRetention, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ARTIFACT_STORAGE_IMPACT_RETENTION_VALUES!r}")
