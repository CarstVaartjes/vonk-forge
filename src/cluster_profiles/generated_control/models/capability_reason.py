from typing import Literal

CapabilityReason = Literal['capability.configuration_invalid', 'capability.dependency_unavailable', 'capability.initializing', 'capability.storage_unavailable']

CAPABILITY_REASON_VALUES: set[CapabilityReason] = { 'capability.configuration_invalid', 'capability.dependency_unavailable', 'capability.initializing', 'capability.storage_unavailable',  }

def check_capability_reason(value: str) -> CapabilityReason:
    if value in CAPABILITY_REASON_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CAPABILITY_REASON_VALUES!r}")
