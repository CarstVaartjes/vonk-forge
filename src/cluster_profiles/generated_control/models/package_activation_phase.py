from typing import Literal

PackageActivationPhase = Literal['acknowledged', 'activation_failed', 'armed', 'rollback_failed', 'rolled_back', 'rolling_back']

PACKAGE_ACTIVATION_PHASE_VALUES: set[PackageActivationPhase] = { 'acknowledged', 'activation_failed', 'armed', 'rollback_failed', 'rolled_back', 'rolling_back',  }

def check_package_activation_phase(value: str) -> PackageActivationPhase:
    if value in PACKAGE_ACTIVATION_PHASE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PACKAGE_ACTIVATION_PHASE_VALUES!r}")
