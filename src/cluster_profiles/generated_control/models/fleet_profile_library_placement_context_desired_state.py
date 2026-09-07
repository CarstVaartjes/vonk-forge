from typing import Literal, cast

FleetProfileLibraryPlacementContextDesiredState = Literal['installed', 'running']

FLEET_PROFILE_LIBRARY_PLACEMENT_CONTEXT_DESIRED_STATE_VALUES: set[FleetProfileLibraryPlacementContextDesiredState] = { 'installed', 'running',  }

def check_fleet_profile_library_placement_context_desired_state(value: str) -> FleetProfileLibraryPlacementContextDesiredState:
    if value in FLEET_PROFILE_LIBRARY_PLACEMENT_CONTEXT_DESIRED_STATE_VALUES:
        return cast(FleetProfileLibraryPlacementContextDesiredState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_LIBRARY_PLACEMENT_CONTEXT_DESIRED_STATE_VALUES!r}")
