from typing import Literal, cast

PlacementNodeMemoryKind = Literal['accelerator', 'host', 'unified']

PLACEMENT_NODE_MEMORY_KIND_VALUES: set[PlacementNodeMemoryKind] = { 'accelerator', 'host', 'unified',  }

def check_placement_node_memory_kind(value: str) -> PlacementNodeMemoryKind:
    if value in PLACEMENT_NODE_MEMORY_KIND_VALUES:
        return cast(PlacementNodeMemoryKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PLACEMENT_NODE_MEMORY_KIND_VALUES!r}")
