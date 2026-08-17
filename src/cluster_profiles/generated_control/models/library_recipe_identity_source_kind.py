from typing import Literal, cast

LibraryRecipeIdentitySourceKind = Literal['global', 'local', 'recipe_library', 'workload_run']

LIBRARY_RECIPE_IDENTITY_SOURCE_KIND_VALUES: set[LibraryRecipeIdentitySourceKind] = { 'global', 'local', 'recipe_library', 'workload_run',  }

def check_library_recipe_identity_source_kind(value: str) -> LibraryRecipeIdentitySourceKind:
    if value in LIBRARY_RECIPE_IDENTITY_SOURCE_KIND_VALUES:
        return cast(LibraryRecipeIdentitySourceKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_RECIPE_IDENTITY_SOURCE_KIND_VALUES!r}")
