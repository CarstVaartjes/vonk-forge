from typing import Literal

MigrationStep = Literal['step-2', 'step-3', 'step-4', 'step-5', 'step-6', 'step-7']

MIGRATION_STEP_VALUES: set[MigrationStep] = { 'step-2', 'step-3', 'step-4', 'step-5', 'step-6', 'step-7',  }

def check_migration_step(value: str) -> MigrationStep:
    if value in MIGRATION_STEP_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MIGRATION_STEP_VALUES!r}")
