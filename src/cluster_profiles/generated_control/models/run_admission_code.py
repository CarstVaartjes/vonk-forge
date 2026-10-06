from typing import Literal

RunAdmissionCode = Literal['run.capacity_busy', 'run.dependencies_stale', 'run.fabric_address_duplicate', 'run.fabric_address_missing', 'run.insufficient_memory', 'run.inventory_missing', 'run.mapping_not_ready', 'run.not_installed', 'run.plan_invalid', 'run.plan_stale', 'run.port_occupied', 'run.rendezvous_port_occupied', 'run.stale_inventory', 'run.target_membership_changed', 'run.unreconciled_lost_rank']

RUN_ADMISSION_CODE_VALUES: set[RunAdmissionCode] = { 'run.capacity_busy', 'run.dependencies_stale', 'run.fabric_address_duplicate', 'run.fabric_address_missing', 'run.insufficient_memory', 'run.inventory_missing', 'run.mapping_not_ready', 'run.not_installed', 'run.plan_invalid', 'run.plan_stale', 'run.port_occupied', 'run.rendezvous_port_occupied', 'run.stale_inventory', 'run.target_membership_changed', 'run.unreconciled_lost_rank',  }

def check_run_admission_code(value: str) -> RunAdmissionCode:
    if value in RUN_ADMISSION_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_ADMISSION_CODE_VALUES!r}")
