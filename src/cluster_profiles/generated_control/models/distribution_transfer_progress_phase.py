from typing import Literal

DistributionTransferProgressPhase = Literal['cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'uninstall', 'verify']

DISTRIBUTION_TRANSFER_PROGRESS_PHASE_VALUES: set[DistributionTransferProgressPhase] = { 'cleanup', 'final_verify', 'prepare', 'start', 'stop', 'transfer', 'uninstall', 'verify',  }

def check_distribution_transfer_progress_phase(value: str) -> DistributionTransferProgressPhase:
    if value in DISTRIBUTION_TRANSFER_PROGRESS_PHASE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {DISTRIBUTION_TRANSFER_PROGRESS_PHASE_VALUES!r}")
