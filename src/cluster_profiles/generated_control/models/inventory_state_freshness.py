from typing import Literal

InventoryStateFreshness = Literal['fresh', 'stale']

INVENTORY_STATE_FRESHNESS_VALUES: set[InventoryStateFreshness] = { 'fresh', 'stale',  }

def check_inventory_state_freshness(value: str) -> InventoryStateFreshness:
    if value in INVENTORY_STATE_FRESHNESS_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {INVENTORY_STATE_FRESHNESS_VALUES!r}")
