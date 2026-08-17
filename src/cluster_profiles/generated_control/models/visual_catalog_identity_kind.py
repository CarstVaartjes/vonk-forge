from typing import Literal, cast

VisualCatalogIdentityKind = Literal['execution-harness', 'model-version', 'patch-bundle', 'runtime-distribution']

VISUAL_CATALOG_IDENTITY_KIND_VALUES: set[VisualCatalogIdentityKind] = { 'execution-harness', 'model-version', 'patch-bundle', 'runtime-distribution',  }

def check_visual_catalog_identity_kind(value: str) -> VisualCatalogIdentityKind:
    if value in VISUAL_CATALOG_IDENTITY_KIND_VALUES:
        return cast(VisualCatalogIdentityKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {VISUAL_CATALOG_IDENTITY_KIND_VALUES!r}")
