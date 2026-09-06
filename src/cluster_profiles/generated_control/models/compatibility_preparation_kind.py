from typing import Literal, cast

CompatibilityPreparationKind = Literal['engine-generation', 'jit', 'tuning']

COMPATIBILITY_PREPARATION_KIND_VALUES: set[CompatibilityPreparationKind] = { 'engine-generation', 'jit', 'tuning',  }

def check_compatibility_preparation_kind(value: str) -> CompatibilityPreparationKind:
    if value in COMPATIBILITY_PREPARATION_KIND_VALUES:
        return cast(CompatibilityPreparationKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {COMPATIBILITY_PREPARATION_KIND_VALUES!r}")
