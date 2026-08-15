from typing import Literal, cast

RecipePresenceDegradedReasonType0 = Literal['external-member', 'installation-not-installed', 'mapping-incomplete', 'missing-ranks', 'rank-incomplete-bytes', 'rank-membership-mismatch', 'rank-not-installed', 'unexpected-ranks']

RECIPE_PRESENCE_DEGRADED_REASON_TYPE_0_VALUES: set[RecipePresenceDegradedReasonType0] = { 'external-member', 'installation-not-installed', 'mapping-incomplete', 'missing-ranks', 'rank-incomplete-bytes', 'rank-membership-mismatch', 'rank-not-installed', 'unexpected-ranks',  }

def check_recipe_presence_degraded_reason_type_0(value: str) -> RecipePresenceDegradedReasonType0:
    if value in RECIPE_PRESENCE_DEGRADED_REASON_TYPE_0_VALUES:
        return cast(RecipePresenceDegradedReasonType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_PRESENCE_DEGRADED_REASON_TYPE_0_VALUES!r}")
