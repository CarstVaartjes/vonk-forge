from typing import Literal, cast

CompiledTopologyMode = Literal['data_parallel', 'distributed', 'hybrid', 'mpi', 'pipeline_parallel', 'ray', 'single', 'tensor_parallel']

COMPILED_TOPOLOGY_MODE_VALUES: set[CompiledTopologyMode] = { 'data_parallel', 'distributed', 'hybrid', 'mpi', 'pipeline_parallel', 'ray', 'single', 'tensor_parallel',  }

def check_compiled_topology_mode(value: str) -> CompiledTopologyMode:
    if value in COMPILED_TOPOLOGY_MODE_VALUES:
        return cast(CompiledTopologyMode, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {COMPILED_TOPOLOGY_MODE_VALUES!r}")
