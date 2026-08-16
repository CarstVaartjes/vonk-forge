from typing import Literal, cast

RecipeSummaryResponseOrigin = Literal['global', 'local', 'recipe_library', 'workload_run']

RECIPE_SUMMARY_RESPONSE_ORIGIN_VALUES: set[RecipeSummaryResponseOrigin] = { 'global', 'local', 'recipe_library', 'workload_run',  }

def check_recipe_summary_response_origin(value: str) -> RecipeSummaryResponseOrigin:
    if value in RECIPE_SUMMARY_RESPONSE_ORIGIN_VALUES:
        return cast(RecipeSummaryResponseOrigin, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_SUMMARY_RESPONSE_ORIGIN_VALUES!r}")
