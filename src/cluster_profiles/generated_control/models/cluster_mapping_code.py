from typing import Literal

ClusterMappingCode = Literal['mapping.actor', 'mapping.endpoint_owner', 'mapping.node_count', 'mapping.node_incompatible', 'mapping.node_unknown', 'mapping.nodes_invalid', 'mapping.option_invalid', 'mapping.parameter_type', 'mapping.parameter_unknown', 'mapping.parameter_value', 'mapping.parameters_invalid', 'mapping.ready_immutable', 'mapping.recipe_unresolved', 'mapping.stale_plan', 'mapping.topology_invalid']

CLUSTER_MAPPING_CODE_VALUES: set[ClusterMappingCode] = { 'mapping.actor', 'mapping.endpoint_owner', 'mapping.node_count', 'mapping.node_incompatible', 'mapping.node_unknown', 'mapping.nodes_invalid', 'mapping.option_invalid', 'mapping.parameter_type', 'mapping.parameter_unknown', 'mapping.parameter_value', 'mapping.parameters_invalid', 'mapping.ready_immutable', 'mapping.recipe_unresolved', 'mapping.stale_plan', 'mapping.topology_invalid',  }

def check_cluster_mapping_code(value: str) -> ClusterMappingCode:
    if value in CLUSTER_MAPPING_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CLUSTER_MAPPING_CODE_VALUES!r}")
