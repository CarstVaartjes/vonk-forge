from typing import Literal

FailureCode = Literal['agent_upgrade_failed', 'artifact_distribution_failed', 'installation_reconciliation_busy', 'operation_cancelled', 'operation_failed', 'recipe_build_failed', 'recipe_install_failed', 'recipe_job_run_failed', 'recipe_reconciliation_dependency_unavailable', 'recipe_start_failed', 'recipe_stop_failed', 'recipe_uninstall_failed', 'retained_container_foreign', 'runtime_observation_unavailable', 'workload.host_memory_exhausted']

FAILURE_CODE_VALUES: set[FailureCode] = { 'agent_upgrade_failed', 'artifact_distribution_failed', 'installation_reconciliation_busy', 'operation_cancelled', 'operation_failed', 'recipe_build_failed', 'recipe_install_failed', 'recipe_job_run_failed', 'recipe_reconciliation_dependency_unavailable', 'recipe_start_failed', 'recipe_stop_failed', 'recipe_uninstall_failed', 'retained_container_foreign', 'runtime_observation_unavailable', 'workload.host_memory_exhausted',  }

def check_failure_code(value: str) -> FailureCode:
    if value in FAILURE_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FAILURE_CODE_VALUES!r}")
