from typing import Literal, cast

FailureDiagnosticsCategory = Literal['capacity', 'digest', 'network', 'platform-policy', 'runtime', 'timeout', 'unknown']

FAILURE_DIAGNOSTICS_CATEGORY_VALUES: set[FailureDiagnosticsCategory] = { 'capacity', 'digest', 'network', 'platform-policy', 'runtime', 'timeout', 'unknown',  }

def check_failure_diagnostics_category(value: str) -> FailureDiagnosticsCategory:
    if value in FAILURE_DIAGNOSTICS_CATEGORY_VALUES:
        return cast(FailureDiagnosticsCategory, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FAILURE_DIAGNOSTICS_CATEGORY_VALUES!r}")
