from typing import Literal

CapabilityAvailability = Literal['available', 'unavailable']

CAPABILITY_AVAILABILITY_VALUES: set[CapabilityAvailability] = { 'available', 'unavailable',  }

def check_capability_availability(value: str) -> CapabilityAvailability:
    if value in CAPABILITY_AVAILABILITY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CAPABILITY_AVAILABILITY_VALUES!r}")
