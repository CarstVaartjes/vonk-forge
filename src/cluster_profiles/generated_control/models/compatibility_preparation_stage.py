from typing import Literal, cast

CompatibilityPreparationStage = Literal['controller-prepare', 'target-prepare']

COMPATIBILITY_PREPARATION_STAGE_VALUES: set[CompatibilityPreparationStage] = { 'controller-prepare', 'target-prepare',  }

def check_compatibility_preparation_stage(value: str) -> CompatibilityPreparationStage:
    if value in COMPATIBILITY_PREPARATION_STAGE_VALUES:
        return cast(CompatibilityPreparationStage, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {COMPATIBILITY_PREPARATION_STAGE_VALUES!r}")
