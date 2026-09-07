from typing import Literal, cast

RunSwitchChildProgressPhaseType0 = Literal['cleanup', 'container-build', 'final_verify', 'model-download', 'prepare', 'runtime-image', 'runtime-install', 'runtime-plan', 'start', 'stop', 'target-copy', 'transfer', 'verify']

RUN_SWITCH_CHILD_PROGRESS_PHASE_TYPE_0_VALUES: set[RunSwitchChildProgressPhaseType0] = { 'cleanup', 'container-build', 'final_verify', 'model-download', 'prepare', 'runtime-image', 'runtime-install', 'runtime-plan', 'start', 'stop', 'target-copy', 'transfer', 'verify',  }

def check_run_switch_child_progress_phase_type_0(value: str) -> RunSwitchChildProgressPhaseType0:
    if value in RUN_SWITCH_CHILD_PROGRESS_PHASE_TYPE_0_VALUES:
        return cast(RunSwitchChildProgressPhaseType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_CHILD_PROGRESS_PHASE_TYPE_0_VALUES!r}")
