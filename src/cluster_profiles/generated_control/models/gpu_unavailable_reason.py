from typing import Literal

GpuUnavailableReason = Literal['gpu.command-failed', 'gpu.command-timeout', 'gpu.invalid-output', 'gpu.unsupported-metrics']

GPU_UNAVAILABLE_REASON_VALUES: set[GpuUnavailableReason] = { 'gpu.command-failed', 'gpu.command-timeout', 'gpu.invalid-output', 'gpu.unsupported-metrics',  }

def check_gpu_unavailable_reason(value: str) -> GpuUnavailableReason:
    if value in GPU_UNAVAILABLE_REASON_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {GPU_UNAVAILABLE_REASON_VALUES!r}")
