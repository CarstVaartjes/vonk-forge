from typing import Literal

ResourceBlockerCode = Literal['resource.capacity_unknown', 'resource.insufficient', 'resource.insufficient_capacity', 'resource.insufficient_capacity_after_stop', 'resource.insufficient_reservation_budget', 'resource.resident_usage_unknown']

RESOURCE_BLOCKER_CODE_VALUES: set[ResourceBlockerCode] = { 'resource.capacity_unknown', 'resource.insufficient', 'resource.insufficient_capacity', 'resource.insufficient_capacity_after_stop', 'resource.insufficient_reservation_budget', 'resource.resident_usage_unknown',  }

def check_resource_blocker_code(value: str) -> ResourceBlockerCode:
    if value in RESOURCE_BLOCKER_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RESOURCE_BLOCKER_CODE_VALUES!r}")
