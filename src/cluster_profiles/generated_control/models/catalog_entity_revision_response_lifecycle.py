from typing import Literal, cast

CatalogEntityRevisionResponseLifecycle = Literal['blocked', 'deprecated', 'draft', 'resolved']

CATALOG_ENTITY_REVISION_RESPONSE_LIFECYCLE_VALUES: set[CatalogEntityRevisionResponseLifecycle] = { 'blocked', 'deprecated', 'draft', 'resolved',  }

def check_catalog_entity_revision_response_lifecycle(value: str) -> CatalogEntityRevisionResponseLifecycle:
    if value in CATALOG_ENTITY_REVISION_RESPONSE_LIFECYCLE_VALUES:
        return cast(CatalogEntityRevisionResponseLifecycle, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CATALOG_ENTITY_REVISION_RESPONSE_LIFECYCLE_VALUES!r}")
