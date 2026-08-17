from typing import Literal, cast

OperationalBuildState = Literal['building', 'failed', 'planned', 'succeeded']

OPERATIONAL_BUILD_STATE_VALUES: set[OperationalBuildState] = { 'building', 'failed', 'planned', 'succeeded',  }

def check_operational_build_state(value: str) -> OperationalBuildState:
    if value in OPERATIONAL_BUILD_STATE_VALUES:
        return cast(OperationalBuildState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OPERATIONAL_BUILD_STATE_VALUES!r}")
