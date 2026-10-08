from typing import Literal

ProfileChildPhase = Literal['cleanup', 'container-build', 'container-download', 'final-verify', 'model-download', 'prepare', 'runtime-install', 'start', 'stop', 'target-copy', 'transfer', 'uninstall', 'verify']

PROFILE_CHILD_PHASE_VALUES: set[ProfileChildPhase] = { 'cleanup', 'container-build', 'container-download', 'final-verify', 'model-download', 'prepare', 'runtime-install', 'start', 'stop', 'target-copy', 'transfer', 'uninstall', 'verify',  }

def check_profile_child_phase(value: str) -> ProfileChildPhase:
    if value in PROFILE_CHILD_PHASE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROFILE_CHILD_PHASE_VALUES!r}")
