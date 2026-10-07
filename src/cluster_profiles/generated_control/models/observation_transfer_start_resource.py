from typing import Literal

ObservationTransferStartResource = Literal['fleet', 'platform']

OBSERVATION_TRANSFER_START_RESOURCE_VALUES: set[ObservationTransferStartResource] = { 'fleet', 'platform',  }

def check_observation_transfer_start_resource(value: str) -> ObservationTransferStartResource:
    if value in OBSERVATION_TRANSFER_START_RESOURCE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OBSERVATION_TRANSFER_START_RESOURCE_VALUES!r}")
