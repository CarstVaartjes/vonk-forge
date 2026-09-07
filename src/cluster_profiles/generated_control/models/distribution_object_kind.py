from typing import Literal, cast

DistributionObjectKind = Literal['model', 'oci-archive', 'oci-layer']

DISTRIBUTION_OBJECT_KIND_VALUES: set[DistributionObjectKind] = { 'model', 'oci-archive', 'oci-layer',  }

def check_distribution_object_kind(value: str) -> DistributionObjectKind:
    if value in DISTRIBUTION_OBJECT_KIND_VALUES:
        return cast(DistributionObjectKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {DISTRIBUTION_OBJECT_KIND_VALUES!r}")
