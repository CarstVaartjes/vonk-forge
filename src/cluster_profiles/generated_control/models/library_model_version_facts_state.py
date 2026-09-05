from typing import Literal, cast

LibraryModelVersionFactsState = Literal['resolved', 'unknown']

LIBRARY_MODEL_VERSION_FACTS_STATE_VALUES: set[LibraryModelVersionFactsState] = { 'resolved', 'unknown',  }

def check_library_model_version_facts_state(value: str) -> LibraryModelVersionFactsState:
    if value in LIBRARY_MODEL_VERSION_FACTS_STATE_VALUES:
        return cast(LibraryModelVersionFactsState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_MODEL_VERSION_FACTS_STATE_VALUES!r}")
