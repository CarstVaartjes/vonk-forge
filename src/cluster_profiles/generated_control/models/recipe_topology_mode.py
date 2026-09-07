from typing import Literal, cast

RecipeTopologyMode = Literal['data_parallel', 'distributed', 'hybrid', 'mpi', 'pipeline_parallel', 'ray', 'single', 'tensor_parallel']

RECIPE_TOPOLOGY_MODE_VALUES: set[RecipeTopologyMode] = { 'data_parallel', 'distributed', 'hybrid', 'mpi', 'pipeline_parallel', 'ray', 'single', 'tensor_parallel',  }

def check_recipe_topology_mode(value: str) -> RecipeTopologyMode:
    if value in RECIPE_TOPOLOGY_MODE_VALUES:
        return cast(RecipeTopologyMode, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_TOPOLOGY_MODE_VALUES!r}")
