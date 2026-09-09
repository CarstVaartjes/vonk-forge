from typing import Literal, cast

StoredRunNodePlanMemoryKind = Literal['accelerator', 'host', 'unified']

STORED_RUN_NODE_PLAN_MEMORY_KIND_VALUES: set[StoredRunNodePlanMemoryKind] = { 'accelerator', 'host', 'unified',  }

def check_stored_run_node_plan_memory_kind(value: str) -> StoredRunNodePlanMemoryKind:
    if value in STORED_RUN_NODE_PLAN_MEMORY_KIND_VALUES:
        return cast(StoredRunNodePlanMemoryKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {STORED_RUN_NODE_PLAN_MEMORY_KIND_VALUES!r}")
