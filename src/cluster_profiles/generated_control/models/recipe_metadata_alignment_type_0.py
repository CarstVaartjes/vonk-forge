from typing import Literal, cast

RecipeMetadataAlignmentType0 = Literal['abliterated', 'derisked', 'other-modified', 'standard', 'unspecified']

RECIPE_METADATA_ALIGNMENT_TYPE_0_VALUES: set[RecipeMetadataAlignmentType0] = { 'abliterated', 'derisked', 'other-modified', 'standard', 'unspecified',  }

def check_recipe_metadata_alignment_type_0(value: str) -> RecipeMetadataAlignmentType0:
    if value in RECIPE_METADATA_ALIGNMENT_TYPE_0_VALUES:
        return cast(RecipeMetadataAlignmentType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_METADATA_ALIGNMENT_TYPE_0_VALUES!r}")
