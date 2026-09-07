from typing import Literal, cast

CompatibilityPreparationState = Literal['failed', 'missing', 'preparing', 'ready', 'unknown', 'unsupported', 'verifying']

COMPATIBILITY_PREPARATION_STATE_VALUES: set[CompatibilityPreparationState] = { 'failed', 'missing', 'preparing', 'ready', 'unknown', 'unsupported', 'verifying',  }

def check_compatibility_preparation_state(value: str) -> CompatibilityPreparationState:
    if value in COMPATIBILITY_PREPARATION_STATE_VALUES:
        return cast(CompatibilityPreparationState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {COMPATIBILITY_PREPARATION_STATE_VALUES!r}")
