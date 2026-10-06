from typing import Literal

DistributionJobPayloadPhase = Literal['cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'uninstall', 'verify']

DISTRIBUTION_JOB_PAYLOAD_PHASE_VALUES: set[DistributionJobPayloadPhase] = { 'cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'uninstall', 'verify',  }

def check_distribution_job_payload_phase(value: str) -> DistributionJobPayloadPhase:
    if value in DISTRIBUTION_JOB_PAYLOAD_PHASE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {DISTRIBUTION_JOB_PAYLOAD_PHASE_VALUES!r}")
