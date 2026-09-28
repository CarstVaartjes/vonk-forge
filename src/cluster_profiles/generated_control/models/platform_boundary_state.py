from typing import Literal

PlatformBoundaryState = Literal['observed', 'publication_not_deployed', 'repository_not_published', 'unknown']

PLATFORM_BOUNDARY_STATE_VALUES: set[PlatformBoundaryState] = { 'observed', 'publication_not_deployed', 'repository_not_published', 'unknown',  }

def check_platform_boundary_state(value: str) -> PlatformBoundaryState:
    if value in PLATFORM_BOUNDARY_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PLATFORM_BOUNDARY_STATE_VALUES!r}")
