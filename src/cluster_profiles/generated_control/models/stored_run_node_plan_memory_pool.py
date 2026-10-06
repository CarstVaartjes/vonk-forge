from typing import Literal

StoredRunNodePlanMemoryPool = Literal['separate', 'shared']

STORED_RUN_NODE_PLAN_MEMORY_POOL_VALUES: set[StoredRunNodePlanMemoryPool] = { 'separate', 'shared',  }

def check_stored_run_node_plan_memory_pool(value: str) -> StoredRunNodePlanMemoryPool:
    if value in STORED_RUN_NODE_PLAN_MEMORY_POOL_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {STORED_RUN_NODE_PLAN_MEMORY_POOL_VALUES!r}")
