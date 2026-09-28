from typing import Literal

RecipeValidationCheckAssertionsItem = Literal['artifact.output', 'chat.nonempty', 'chat.output-cap', 'completion.nonempty', 'completion.output-cap', 'embedding.nonempty', 'endpoint.healthy', 'inference.completed', 'tools.called']

RECIPE_VALIDATION_CHECK_ASSERTIONS_ITEM_VALUES: set[RecipeValidationCheckAssertionsItem] = { 'artifact.output', 'chat.nonempty', 'chat.output-cap', 'completion.nonempty', 'completion.output-cap', 'embedding.nonempty', 'endpoint.healthy', 'inference.completed', 'tools.called',  }

def check_recipe_validation_check_assertions_item(value: str) -> RecipeValidationCheckAssertionsItem:
    if value in RECIPE_VALIDATION_CHECK_ASSERTIONS_ITEM_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_VALIDATION_CHECK_ASSERTIONS_ITEM_VALUES!r}")
