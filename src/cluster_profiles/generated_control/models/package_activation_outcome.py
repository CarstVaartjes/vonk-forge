from typing import Literal

PackageActivationOutcome = Literal['awaiting_controller_activation', 'candidate_install_failed', 'controller_confirmed_activation', 'restoring_captured_source', 'source_restore_failed', 'source_restored_and_restarted']

PACKAGE_ACTIVATION_OUTCOME_VALUES: set[PackageActivationOutcome] = { 'awaiting_controller_activation', 'candidate_install_failed', 'controller_confirmed_activation', 'restoring_captured_source', 'source_restore_failed', 'source_restored_and_restarted',  }

def check_package_activation_outcome(value: str) -> PackageActivationOutcome:
    if value in PACKAGE_ACTIVATION_OUTCOME_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PACKAGE_ACTIVATION_OUTCOME_VALUES!r}")
