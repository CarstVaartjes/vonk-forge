from typing import Literal

RecipeBuildIntentKind = Literal['dependency', 'independent']

RECIPE_BUILD_INTENT_KIND_VALUES: set[RecipeBuildIntentKind] = { 'dependency', 'independent',  }

def check_recipe_build_intent_kind(value: str) -> RecipeBuildIntentKind:
    if value in RECIPE_BUILD_INTENT_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_BUILD_INTENT_KIND_VALUES!r}")
