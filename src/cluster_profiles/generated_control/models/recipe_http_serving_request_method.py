from typing import Literal, cast

RecipeHttpServingRequestMethod = Literal['GET', 'POST']

RECIPE_HTTP_SERVING_REQUEST_METHOD_VALUES: set[RecipeHttpServingRequestMethod] = { 'GET', 'POST',  }

def check_recipe_http_serving_request_method(value: str) -> RecipeHttpServingRequestMethod:
    if value in RECIPE_HTTP_SERVING_REQUEST_METHOD_VALUES:
        return cast(RecipeHttpServingRequestMethod, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_HTTP_SERVING_REQUEST_METHOD_VALUES!r}")
