from typing import Literal, cast

FleetProfileChildProgressPhase = Literal['cleanup', 'container-build', 'container-download', 'final-verify', 'final_verify', 'model-download', 'prepare', 'runtime-install', 'start', 'stop', 'target-copy', 'transfer', 'verify']

FLEET_PROFILE_CHILD_PROGRESS_PHASE_VALUES: set[FleetProfileChildProgressPhase] = { 'cleanup', 'container-build', 'container-download', 'final-verify', 'final_verify', 'model-download', 'prepare', 'runtime-install', 'start', 'stop', 'target-copy', 'transfer', 'verify',  }

def check_fleet_profile_child_progress_phase(value: str) -> FleetProfileChildProgressPhase:
    if value in FLEET_PROFILE_CHILD_PROGRESS_PHASE_VALUES:
        return cast(FleetProfileChildProgressPhase, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_CHILD_PROGRESS_PHASE_VALUES!r}")
