from typing import Literal, cast

CompiledDistributionObjectKind = Literal['model', 'oci-archive']

COMPILED_DISTRIBUTION_OBJECT_KIND_VALUES: set[CompiledDistributionObjectKind] = { 'model', 'oci-archive',  }

def check_compiled_distribution_object_kind(value: str) -> CompiledDistributionObjectKind:
    if value in COMPILED_DISTRIBUTION_OBJECT_KIND_VALUES:
        return cast(CompiledDistributionObjectKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {COMPILED_DISTRIBUTION_OBJECT_KIND_VALUES!r}")
