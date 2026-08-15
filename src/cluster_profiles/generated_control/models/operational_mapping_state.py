from typing import Literal, cast

OperationalMappingState = Literal['planned', 'ready', 'stale']

OPERATIONAL_MAPPING_STATE_VALUES: set[OperationalMappingState] = { 'planned', 'ready', 'stale',  }

def check_operational_mapping_state(value: str) -> OperationalMappingState:
    if value in OPERATIONAL_MAPPING_STATE_VALUES:
        return cast(OperationalMappingState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OPERATIONAL_MAPPING_STATE_VALUES!r}")
