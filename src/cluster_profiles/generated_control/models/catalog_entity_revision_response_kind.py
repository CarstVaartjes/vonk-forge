from typing import Literal, cast

CatalogEntityRevisionResponseKind = Literal['execution-harness', 'model', 'model-group', 'model-version', 'patch-bundle', 'runtime-distribution']

CATALOG_ENTITY_REVISION_RESPONSE_KIND_VALUES: set[CatalogEntityRevisionResponseKind] = { 'execution-harness', 'model', 'model-group', 'model-version', 'patch-bundle', 'runtime-distribution',  }

def check_catalog_entity_revision_response_kind(value: str) -> CatalogEntityRevisionResponseKind:
    if value in CATALOG_ENTITY_REVISION_RESPONSE_KIND_VALUES:
        return cast(CatalogEntityRevisionResponseKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CATALOG_ENTITY_REVISION_RESPONSE_KIND_VALUES!r}")
