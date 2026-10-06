from typing import Literal

ProgressPhase = Literal['building', 'completed', 'copying', 'downloading', 'executing', 'failed', 'finalizing', 'installing', 'model-download', 'pending', 'preparing', 'pulling', 'queued', 'reclaiming', 'reconciling-installation', 'starting', 'stopping', 'transfer', 'uninstalling', 'updating', 'uploading', 'verifying', 'waiting']

PROGRESS_PHASE_VALUES: set[ProgressPhase] = { 'building', 'completed', 'copying', 'downloading', 'executing', 'failed', 'finalizing', 'installing', 'model-download', 'pending', 'preparing', 'pulling', 'queued', 'reclaiming', 'reconciling-installation', 'starting', 'stopping', 'transfer', 'uninstalling', 'updating', 'uploading', 'verifying', 'waiting',  }

def check_progress_phase(value: str) -> ProgressPhase:
    if value in PROGRESS_PHASE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROGRESS_PHASE_VALUES!r}")
