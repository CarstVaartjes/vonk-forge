from typing import Literal

TopologyCode = Literal['topology.fabric_insufficient', 'topology.invalid', 'topology.placement_invalid', 'topology.role_mismatch', 'topology.runtime_capability_missing']

TOPOLOGY_CODE_VALUES: set[TopologyCode] = { 'topology.fabric_insufficient', 'topology.invalid', 'topology.placement_invalid', 'topology.role_mismatch', 'topology.runtime_capability_missing',  }

def check_topology_code(value: str) -> TopologyCode:
    if value in TOPOLOGY_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TOPOLOGY_CODE_VALUES!r}")
