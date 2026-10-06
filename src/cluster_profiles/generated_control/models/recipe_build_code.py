from typing import Literal

RecipeBuildCode = Literal['build.adapter_unavailable', 'build.cancellation_pending', 'build.capability_missing', 'build.capacity_busy', 'build.capacity_contract_invalid', 'build.consumer_busy', 'build.consumer_invalid', 'build.contract_invalid', 'build.dependencies_stale', 'build.evidence_invalid', 'build.image_size_invalid', 'build.input_mismatch', 'build.insufficient_disk', 'build.insufficient_memory', 'build.inventory_missing', 'build.inventory_stale', 'build.network_capability_missing', 'build.node_incompatible', 'build.node_unknown', 'build.plan_invalid', 'build.producer_invalid', 'build.recipe_unresolved', 'build.resolution_stale', 'build.resources_invalid', 'build.result_conflict', 'build.runtime_changed', 'build.security_invalid', 'build.shared_consumers', 'build.source_invalid', 'build.source_unavailable', 'build.state']

RECIPE_BUILD_CODE_VALUES: set[RecipeBuildCode] = { 'build.adapter_unavailable', 'build.cancellation_pending', 'build.capability_missing', 'build.capacity_busy', 'build.capacity_contract_invalid', 'build.consumer_busy', 'build.consumer_invalid', 'build.contract_invalid', 'build.dependencies_stale', 'build.evidence_invalid', 'build.image_size_invalid', 'build.input_mismatch', 'build.insufficient_disk', 'build.insufficient_memory', 'build.inventory_missing', 'build.inventory_stale', 'build.network_capability_missing', 'build.node_incompatible', 'build.node_unknown', 'build.plan_invalid', 'build.producer_invalid', 'build.recipe_unresolved', 'build.resolution_stale', 'build.resources_invalid', 'build.result_conflict', 'build.runtime_changed', 'build.security_invalid', 'build.shared_consumers', 'build.source_invalid', 'build.source_unavailable', 'build.state',  }

def check_recipe_build_code(value: str) -> RecipeBuildCode:
    if value in RECIPE_BUILD_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_BUILD_CODE_VALUES!r}")
