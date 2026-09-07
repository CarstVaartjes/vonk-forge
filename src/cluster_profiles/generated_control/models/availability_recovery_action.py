from typing import Literal, cast

AvailabilityRecoveryAction = Literal['check_access_and_resume', 'configure_hf_token', 'download_again', 'force_rebuild', 'free_space', 'inspect', 'open_model_access', 'resume', 'retry']

AVAILABILITY_RECOVERY_ACTION_VALUES: set[AvailabilityRecoveryAction] = { 'check_access_and_resume', 'configure_hf_token', 'download_again', 'force_rebuild', 'free_space', 'inspect', 'open_model_access', 'resume', 'retry',  }

def check_availability_recovery_action(value: str) -> AvailabilityRecoveryAction:
    if value in AVAILABILITY_RECOVERY_ACTION_VALUES:
        return cast(AvailabilityRecoveryAction, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {AVAILABILITY_RECOVERY_ACTION_VALUES!r}")
