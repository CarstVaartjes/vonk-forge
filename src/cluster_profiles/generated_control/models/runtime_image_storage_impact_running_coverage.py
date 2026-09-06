from typing import Literal, cast

RuntimeImageStorageImpactRunningCoverage = Literal['complete', 'partial', 'unknown']

RUNTIME_IMAGE_STORAGE_IMPACT_RUNNING_COVERAGE_VALUES: set[RuntimeImageStorageImpactRunningCoverage] = { 'complete', 'partial', 'unknown',  }

def check_runtime_image_storage_impact_running_coverage(value: str) -> RuntimeImageStorageImpactRunningCoverage:
    if value in RUNTIME_IMAGE_STORAGE_IMPACT_RUNNING_COVERAGE_VALUES:
        return cast(RuntimeImageStorageImpactRunningCoverage, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUNTIME_IMAGE_STORAGE_IMPACT_RUNNING_COVERAGE_VALUES!r}")
