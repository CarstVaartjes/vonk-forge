from typing import Literal, cast

FleetProfileResourceRequirementMemoryKindType0 = Literal['accelerator', 'host', 'unified']

FLEET_PROFILE_RESOURCE_REQUIREMENT_MEMORY_KIND_TYPE_0_VALUES: set[FleetProfileResourceRequirementMemoryKindType0] = { 'accelerator', 'host', 'unified',  }

def check_fleet_profile_resource_requirement_memory_kind_type_0(value: str) -> FleetProfileResourceRequirementMemoryKindType0:
    if value in FLEET_PROFILE_RESOURCE_REQUIREMENT_MEMORY_KIND_TYPE_0_VALUES:
        return cast(FleetProfileResourceRequirementMemoryKindType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_RESOURCE_REQUIREMENT_MEMORY_KIND_TYPE_0_VALUES!r}")
