from typing import Literal, cast

LibraryCatalogReferenceKind = Literal['model', 'model-group', 'model-version']

LIBRARY_CATALOG_REFERENCE_KIND_VALUES: set[LibraryCatalogReferenceKind] = { 'model', 'model-group', 'model-version',  }

def check_library_catalog_reference_kind(value: str) -> LibraryCatalogReferenceKind:
    if value in LIBRARY_CATALOG_REFERENCE_KIND_VALUES:
        return cast(LibraryCatalogReferenceKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_CATALOG_REFERENCE_KIND_VALUES!r}")
