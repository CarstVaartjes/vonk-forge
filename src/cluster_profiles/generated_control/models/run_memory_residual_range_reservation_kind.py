from typing import Literal

RunMemoryResidualRangeReservationKind = Literal['gpu-memory', 'host-memory', 'unified-memory']

RUN_MEMORY_RESIDUAL_RANGE_RESERVATION_KIND_VALUES: set[RunMemoryResidualRangeReservationKind] = { 'gpu-memory', 'host-memory', 'unified-memory',  }

def check_run_memory_residual_range_reservation_kind(value: str) -> RunMemoryResidualRangeReservationKind:
    if value in RUN_MEMORY_RESIDUAL_RANGE_RESERVATION_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_MEMORY_RESIDUAL_RANGE_RESERVATION_KIND_VALUES!r}")
