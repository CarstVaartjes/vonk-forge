from typing import Literal, cast

PublicRecipePreviewResponseExecutionReadiness = Literal['executable', 'integration-required', 'not-declared', 'not-executable']

PUBLIC_RECIPE_PREVIEW_RESPONSE_EXECUTION_READINESS_VALUES: set[PublicRecipePreviewResponseExecutionReadiness] = { 'executable', 'integration-required', 'not-declared', 'not-executable',  }

def check_public_recipe_preview_response_execution_readiness(value: str) -> PublicRecipePreviewResponseExecutionReadiness:
    if value in PUBLIC_RECIPE_PREVIEW_RESPONSE_EXECUTION_READINESS_VALUES:
        return cast(PublicRecipePreviewResponseExecutionReadiness, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_PREVIEW_RESPONSE_EXECUTION_READINESS_VALUES!r}")
