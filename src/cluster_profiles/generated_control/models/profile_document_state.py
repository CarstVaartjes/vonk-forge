from typing import Literal

ProfileDocumentState = Literal['active', 'draft', 'loaded', 'not-created', 'ready']

PROFILE_DOCUMENT_STATE_VALUES: set[ProfileDocumentState] = { 'active', 'draft', 'loaded', 'not-created', 'ready',  }

def check_profile_document_state(value: str) -> ProfileDocumentState:
    if value in PROFILE_DOCUMENT_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PROFILE_DOCUMENT_STATE_VALUES!r}")
