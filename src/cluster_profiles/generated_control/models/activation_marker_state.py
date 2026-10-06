from typing import Literal

ActivationMarkerState = Literal['maintenance', 'published']

ACTIVATION_MARKER_STATE_VALUES: set[ActivationMarkerState] = { 'maintenance', 'published',  }

def check_activation_marker_state(value: str) -> ActivationMarkerState:
    if value in ACTIVATION_MARKER_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ACTIVATION_MARKER_STATE_VALUES!r}")
