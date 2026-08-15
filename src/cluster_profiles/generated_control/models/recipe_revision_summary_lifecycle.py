from typing import Literal, cast

RecipeRevisionSummaryLifecycle = Literal['blocked', 'deprecated', 'draft', 'resolved']

RECIPE_REVISION_SUMMARY_LIFECYCLE_VALUES: set[RecipeRevisionSummaryLifecycle] = { 'blocked', 'deprecated', 'draft', 'resolved',  }

def check_recipe_revision_summary_lifecycle(value: str) -> RecipeRevisionSummaryLifecycle:
    if value in RECIPE_REVISION_SUMMARY_LIFECYCLE_VALUES:
        return cast(RecipeRevisionSummaryLifecycle, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_REVISION_SUMMARY_LIFECYCLE_VALUES!r}")
