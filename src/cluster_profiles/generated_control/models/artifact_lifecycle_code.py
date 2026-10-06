from typing import Literal

ArtifactLifecycleCode = Literal['artifact.asset_availability_unknown', 'artifact.deletion_busy', 'artifact.deletion_fence_lost', 'artifact.deletion_in_progress', 'artifact.reference_busy', 'artifact.reference_changed', 'artifact.reference_identity_mismatch', 'artifact.reference_scan_failed', 'artifact.reference_scan_limited', 'artifact.reference_timeout', 'artifact.reference_unavailable', 'artifact.removal_owner_invalid', 'artifact.removal_owner_unresolved']

ARTIFACT_LIFECYCLE_CODE_VALUES: set[ArtifactLifecycleCode] = { 'artifact.asset_availability_unknown', 'artifact.deletion_busy', 'artifact.deletion_fence_lost', 'artifact.deletion_in_progress', 'artifact.reference_busy', 'artifact.reference_changed', 'artifact.reference_identity_mismatch', 'artifact.reference_scan_failed', 'artifact.reference_scan_limited', 'artifact.reference_timeout', 'artifact.reference_unavailable', 'artifact.removal_owner_invalid', 'artifact.removal_owner_unresolved',  }

def check_artifact_lifecycle_code(value: str) -> ArtifactLifecycleCode:
    if value in ARTIFACT_LIFECYCLE_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ARTIFACT_LIFECYCLE_CODE_VALUES!r}")
