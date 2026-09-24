from typing import Literal, cast

SparkFitNodeMemoryPoolType0 = Literal['separate', 'shared']

SPARK_FIT_NODE_MEMORY_POOL_TYPE_0_VALUES: set[SparkFitNodeMemoryPoolType0] = { 'separate', 'shared',  }

def check_spark_fit_node_memory_pool_type_0(value: str) -> SparkFitNodeMemoryPoolType0:
    if value in SPARK_FIT_NODE_MEMORY_POOL_TYPE_0_VALUES:
        return cast(SparkFitNodeMemoryPoolType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {SPARK_FIT_NODE_MEMORY_POOL_TYPE_0_VALUES!r}")
