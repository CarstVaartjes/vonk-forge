from typing import Literal, cast

RuntimeImageStorageImpactSparkCoverage = Literal['complete', 'partial', 'unknown']

RUNTIME_IMAGE_STORAGE_IMPACT_SPARK_COVERAGE_VALUES: set[RuntimeImageStorageImpactSparkCoverage] = { 'complete', 'partial', 'unknown',  }

def check_runtime_image_storage_impact_spark_coverage(value: str) -> RuntimeImageStorageImpactSparkCoverage:
    if value in RUNTIME_IMAGE_STORAGE_IMPACT_SPARK_COVERAGE_VALUES:
        return cast(RuntimeImageStorageImpactSparkCoverage, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUNTIME_IMAGE_STORAGE_IMPACT_SPARK_COVERAGE_VALUES!r}")
