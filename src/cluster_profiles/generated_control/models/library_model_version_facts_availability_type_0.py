from typing import Literal, cast

LibraryModelVersionFactsAvailabilityType0 = Literal['active', 'superseded', 'withdrawn']

LIBRARY_MODEL_VERSION_FACTS_AVAILABILITY_TYPE_0_VALUES: set[LibraryModelVersionFactsAvailabilityType0] = { 'active', 'superseded', 'withdrawn',  }

def check_library_model_version_facts_availability_type_0(value: str) -> LibraryModelVersionFactsAvailabilityType0:
    if value in LIBRARY_MODEL_VERSION_FACTS_AVAILABILITY_TYPE_0_VALUES:
        return cast(LibraryModelVersionFactsAvailabilityType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_MODEL_VERSION_FACTS_AVAILABILITY_TYPE_0_VALUES!r}")
