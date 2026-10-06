from typing import Literal

RecipePackageCode = Literal['recipe_package.cache_unavailable', 'recipe_package.digest_mismatch', 'recipe_package.document_incompatible', 'recipe_package.extract_invalid', 'recipe_package.not_found', 'recipe_package.package_invalid', 'recipe_package.release_incomplete', 'recipe_package.release_invalid', 'recipe_package.response_invalid', 'recipe_package.schema_incompatible', 'recipe_package.snapshot_changed', 'recipe_package.unavailable', 'recipe_package.uri_invalid', 'recipe_package.url_insecure', 'recipe_package.url_invalid']

RECIPE_PACKAGE_CODE_VALUES: set[RecipePackageCode] = { 'recipe_package.cache_unavailable', 'recipe_package.digest_mismatch', 'recipe_package.document_incompatible', 'recipe_package.extract_invalid', 'recipe_package.not_found', 'recipe_package.package_invalid', 'recipe_package.release_incomplete', 'recipe_package.release_invalid', 'recipe_package.response_invalid', 'recipe_package.schema_incompatible', 'recipe_package.snapshot_changed', 'recipe_package.unavailable', 'recipe_package.uri_invalid', 'recipe_package.url_insecure', 'recipe_package.url_invalid',  }

def check_recipe_package_code(value: str) -> RecipePackageCode:
    if value in RECIPE_PACKAGE_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_PACKAGE_CODE_VALUES!r}")
