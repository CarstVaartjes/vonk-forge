from typing import Literal, cast

RecipeProvenanceSourceKind = Literal['fork', 'global', 'local', 'workload_run']

RECIPE_PROVENANCE_SOURCE_KIND_VALUES: set[RecipeProvenanceSourceKind] = { 'fork', 'global', 'local', 'workload_run',  }

def check_recipe_provenance_source_kind(value: str) -> RecipeProvenanceSourceKind:
    if value in RECIPE_PROVENANCE_SOURCE_KIND_VALUES:
        return cast(RecipeProvenanceSourceKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_PROVENANCE_SOURCE_KIND_VALUES!r}")
