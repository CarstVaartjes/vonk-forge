from typing import Literal

PlatformBoundaryBoundary = Literal['controller_deployment', 'publication', 'repository']

PLATFORM_BOUNDARY_BOUNDARY_VALUES: set[PlatformBoundaryBoundary] = { 'controller_deployment', 'publication', 'repository',  }

def check_platform_boundary_boundary(value: str) -> PlatformBoundaryBoundary:
    if value in PLATFORM_BOUNDARY_BOUNDARY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PLATFORM_BOUNDARY_BOUNDARY_VALUES!r}")
