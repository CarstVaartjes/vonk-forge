from typing import Literal

DistributionCode = Literal['distribution.assignment_conflict', 'distribution.expired', 'distribution.model_set_identity_unavailable', 'distribution.model_set_mismatch', 'distribution.object_invalid', 'distribution.object_unavailable', 'distribution.runtime_image_mismatch', 'distribution.unassigned', 'distribution.wrong_node']

DISTRIBUTION_CODE_VALUES: set[DistributionCode] = { 'distribution.assignment_conflict', 'distribution.expired', 'distribution.model_set_identity_unavailable', 'distribution.model_set_mismatch', 'distribution.object_invalid', 'distribution.object_unavailable', 'distribution.runtime_image_mismatch', 'distribution.unassigned', 'distribution.wrong_node',  }

def check_distribution_code(value: str) -> DistributionCode:
    if value in DISTRIBUTION_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {DISTRIBUTION_CODE_VALUES!r}")
