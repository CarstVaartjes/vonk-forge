from typing import Literal

PlacementLoadState = Literal['loaded', 'not_loaded', 'unknown']

PLACEMENT_LOAD_STATE_VALUES: set[PlacementLoadState] = { 'loaded', 'not_loaded', 'unknown',  }

def check_placement_load_state(value: str) -> PlacementLoadState:
    if value in PLACEMENT_LOAD_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PLACEMENT_LOAD_STATE_VALUES!r}")
