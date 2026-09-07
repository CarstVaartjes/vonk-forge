from typing import Literal, cast

RecipeFailurePolicyRecovery = Literal['restart-entrypoint', 'restart-worker-then-entrypoint']

RECIPE_FAILURE_POLICY_RECOVERY_VALUES: set[RecipeFailurePolicyRecovery] = { 'restart-entrypoint', 'restart-worker-then-entrypoint',  }

def check_recipe_failure_policy_recovery(value: str) -> RecipeFailurePolicyRecovery:
    if value in RECIPE_FAILURE_POLICY_RECOVERY_VALUES:
        return cast(RecipeFailurePolicyRecovery, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_FAILURE_POLICY_RECOVERY_VALUES!r}")
