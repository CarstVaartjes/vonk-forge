from typing import Literal, cast

LibraryModelLineageRelation = Literal['derived', 'official', 'quantized']

LIBRARY_MODEL_LINEAGE_RELATION_VALUES: set[LibraryModelLineageRelation] = { 'derived', 'official', 'quantized',  }

def check_library_model_lineage_relation(value: str) -> LibraryModelLineageRelation:
    if value in LIBRARY_MODEL_LINEAGE_RELATION_VALUES:
        return cast(LibraryModelLineageRelation, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_MODEL_LINEAGE_RELATION_VALUES!r}")
