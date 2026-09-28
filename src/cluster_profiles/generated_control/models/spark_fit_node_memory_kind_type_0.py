from typing import Literal

SparkFitNodeMemoryKindType0 = Literal['accelerator', 'host', 'unified']

SPARK_FIT_NODE_MEMORY_KIND_TYPE_0_VALUES: set[SparkFitNodeMemoryKindType0] = { 'accelerator', 'host', 'unified',  }

def check_spark_fit_node_memory_kind_type_0(value: str) -> SparkFitNodeMemoryKindType0:
    if value in SPARK_FIT_NODE_MEMORY_KIND_TYPE_0_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {SPARK_FIT_NODE_MEMORY_KIND_TYPE_0_VALUES!r}")
