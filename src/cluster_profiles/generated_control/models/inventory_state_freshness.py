from typing import Literal, cast

InventoryStateFreshness = Literal['fresh', 'stale']

INVENTORY_STATE_FRESHNESS_VALUES: set[InventoryStateFreshness] = { 'fresh', 'stale',  }

def check_inventory_state_freshness(value: str) -> InventoryStateFreshness:
    if value in INVENTORY_STATE_FRESHNESS_VALUES:
        return cast(InventoryStateFreshness, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {INVENTORY_STATE_FRESHNESS_VALUES!r}")
