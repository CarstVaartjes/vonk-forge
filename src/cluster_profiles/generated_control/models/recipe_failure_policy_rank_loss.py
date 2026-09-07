from typing import Literal, cast

RecipeFailurePolicyRankLoss = Literal['not-applicable', 'withdraw-endpoint']

RECIPE_FAILURE_POLICY_RANK_LOSS_VALUES: set[RecipeFailurePolicyRankLoss] = { 'not-applicable', 'withdraw-endpoint',  }

def check_recipe_failure_policy_rank_loss(value: str) -> RecipeFailurePolicyRankLoss:
    if value in RECIPE_FAILURE_POLICY_RANK_LOSS_VALUES:
        return cast(RecipeFailurePolicyRankLoss, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_FAILURE_POLICY_RANK_LOSS_VALUES!r}")
