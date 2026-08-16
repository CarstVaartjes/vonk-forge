from typing import Literal, cast

LibraryRecipeSummarySourceKind = Literal['global', 'local', 'recipe_library', 'workload_run']

LIBRARY_RECIPE_SUMMARY_SOURCE_KIND_VALUES: set[LibraryRecipeSummarySourceKind] = { 'global', 'local', 'recipe_library', 'workload_run',  }

def check_library_recipe_summary_source_kind(value: str) -> LibraryRecipeSummarySourceKind:
    if value in LIBRARY_RECIPE_SUMMARY_SOURCE_KIND_VALUES:
        return cast(LibraryRecipeSummarySourceKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_RECIPE_SUMMARY_SOURCE_KIND_VALUES!r}")
