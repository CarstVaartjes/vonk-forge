from typing import Literal, cast

PublicRecipePreviewResponseExecutionReadinessBasis = Literal['conflicting-readiness-metadata', 'explicit-executable-metadata', 'explicit-integration-required-metadata', 'explicit-non-executable-metadata', 'missing-readiness-metadata']

PUBLIC_RECIPE_PREVIEW_RESPONSE_EXECUTION_READINESS_BASIS_VALUES: set[PublicRecipePreviewResponseExecutionReadinessBasis] = { 'conflicting-readiness-metadata', 'explicit-executable-metadata', 'explicit-integration-required-metadata', 'explicit-non-executable-metadata', 'missing-readiness-metadata',  }

def check_public_recipe_preview_response_execution_readiness_basis(value: str) -> PublicRecipePreviewResponseExecutionReadinessBasis:
    if value in PUBLIC_RECIPE_PREVIEW_RESPONSE_EXECUTION_READINESS_BASIS_VALUES:
        return cast(PublicRecipePreviewResponseExecutionReadinessBasis, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PUBLIC_RECIPE_PREVIEW_RESPONSE_EXECUTION_READINESS_BASIS_VALUES!r}")
