from typing import Literal, cast

RuntimeImageStorageImpactNasCoverage = Literal['complete', 'partial', 'unknown']

RUNTIME_IMAGE_STORAGE_IMPACT_NAS_COVERAGE_VALUES: set[RuntimeImageStorageImpactNasCoverage] = { 'complete', 'partial', 'unknown',  }

def check_runtime_image_storage_impact_nas_coverage(value: str) -> RuntimeImageStorageImpactNasCoverage:
    if value in RUNTIME_IMAGE_STORAGE_IMPACT_NAS_COVERAGE_VALUES:
        return cast(RuntimeImageStorageImpactNasCoverage, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUNTIME_IMAGE_STORAGE_IMPACT_NAS_COVERAGE_VALUES!r}")
