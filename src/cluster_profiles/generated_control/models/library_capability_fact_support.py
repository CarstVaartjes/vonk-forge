from typing import Literal, cast

LibraryCapabilityFactSupport = Literal['supported', 'unknown', 'unsupported']

LIBRARY_CAPABILITY_FACT_SUPPORT_VALUES: set[LibraryCapabilityFactSupport] = { 'supported', 'unknown', 'unsupported',  }

def check_library_capability_fact_support(value: str) -> LibraryCapabilityFactSupport:
    if value in LIBRARY_CAPABILITY_FACT_SUPPORT_VALUES:
        return cast(LibraryCapabilityFactSupport, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_CAPABILITY_FACT_SUPPORT_VALUES!r}")
