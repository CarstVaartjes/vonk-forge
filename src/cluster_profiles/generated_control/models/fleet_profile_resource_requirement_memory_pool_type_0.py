from typing import Literal, cast

FleetProfileResourceRequirementMemoryPoolType0 = Literal['separate', 'shared']

FLEET_PROFILE_RESOURCE_REQUIREMENT_MEMORY_POOL_TYPE_0_VALUES: set[FleetProfileResourceRequirementMemoryPoolType0] = { 'separate', 'shared',  }

def check_fleet_profile_resource_requirement_memory_pool_type_0(value: str) -> FleetProfileResourceRequirementMemoryPoolType0:
    if value in FLEET_PROFILE_RESOURCE_REQUIREMENT_MEMORY_POOL_TYPE_0_VALUES:
        return cast(FleetProfileResourceRequirementMemoryPoolType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_RESOURCE_REQUIREMENT_MEMORY_POOL_TYPE_0_VALUES!r}")
