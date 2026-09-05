from typing import Literal, cast

LibraryCapabilityProvenanceSourceKind = Literal['model-version', 'recipe-revision']

LIBRARY_CAPABILITY_PROVENANCE_SOURCE_KIND_VALUES: set[LibraryCapabilityProvenanceSourceKind] = { 'model-version', 'recipe-revision',  }

def check_library_capability_provenance_source_kind(value: str) -> LibraryCapabilityProvenanceSourceKind:
    if value in LIBRARY_CAPABILITY_PROVENANCE_SOURCE_KIND_VALUES:
        return cast(LibraryCapabilityProvenanceSourceKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_CAPABILITY_PROVENANCE_SOURCE_KIND_VALUES!r}")
