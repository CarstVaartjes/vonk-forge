from typing import Literal

ProfileReportedPhase = Literal['final_verify']

PROFILE_REPORTED_PHASE_VALUES: set[ProfileReportedPhase] = { 'final_verify',  }

def check_profile_reported_phase(value: str) -> ProfileReportedPhase:
    if value in PROFILE_REPORTED_PHASE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROFILE_REPORTED_PHASE_VALUES!r}")
