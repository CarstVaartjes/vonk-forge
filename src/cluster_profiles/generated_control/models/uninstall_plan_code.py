from typing import Literal

UninstallPlanCode = Literal['uninstall.abandon-never-installed', 'uninstall.active_run', 'uninstall.active_runs_truncated', 'uninstall.bytes_unknown', 'uninstall.installation_not_uninstallable', 'uninstall.operation_active', 'uninstall.rank_membership_changed']

UNINSTALL_PLAN_CODE_VALUES: set[UninstallPlanCode] = { 'uninstall.abandon-never-installed', 'uninstall.active_run', 'uninstall.active_runs_truncated', 'uninstall.bytes_unknown', 'uninstall.installation_not_uninstallable', 'uninstall.operation_active', 'uninstall.rank_membership_changed',  }

def check_uninstall_plan_code(value: str) -> UninstallPlanCode:
    if value in UNINSTALL_PLAN_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {UNINSTALL_PLAN_CODE_VALUES!r}")
