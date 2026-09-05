from typing import Literal, cast

LibraryCapabilityInventoryState = Literal['contradictory', 'declared', 'unknown']

LIBRARY_CAPABILITY_INVENTORY_STATE_VALUES: set[LibraryCapabilityInventoryState] = { 'contradictory', 'declared', 'unknown',  }

def check_library_capability_inventory_state(value: str) -> LibraryCapabilityInventoryState:
    if value in LIBRARY_CAPABILITY_INVENTORY_STATE_VALUES:
        return cast(LibraryCapabilityInventoryState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_CAPABILITY_INVENTORY_STATE_VALUES!r}")
