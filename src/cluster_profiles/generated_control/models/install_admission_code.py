from typing import Literal

InstallAdmissionCode = Literal['install.agent_upgrade_required', 'install.artifact_size_underdeclared', 'install.artifact_store_read_only', 'install.capacity_busy', 'install.compiled_plan_unavailable', 'install.dependencies_stale', 'install.image_distribution_pending', 'install.image_size_underdeclared', 'install.insufficient_disk', 'install.inventory_missing', 'install.model_identity_unavailable', 'install.plan_invalid', 'install.plan_stale', 'install.stale_inventory']

INSTALL_ADMISSION_CODE_VALUES: set[InstallAdmissionCode] = { 'install.agent_upgrade_required', 'install.artifact_size_underdeclared', 'install.artifact_store_read_only', 'install.capacity_busy', 'install.compiled_plan_unavailable', 'install.dependencies_stale', 'install.image_distribution_pending', 'install.image_size_underdeclared', 'install.insufficient_disk', 'install.inventory_missing', 'install.model_identity_unavailable', 'install.plan_invalid', 'install.plan_stale', 'install.stale_inventory',  }

def check_install_admission_code(value: str) -> InstallAdmissionCode:
    if value in INSTALL_ADMISSION_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {INSTALL_ADMISSION_CODE_VALUES!r}")
