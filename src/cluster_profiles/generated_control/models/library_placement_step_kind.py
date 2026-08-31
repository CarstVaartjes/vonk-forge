from typing import Literal, cast

LibraryPlacementStepKind = Literal['build', 'create-placement', 'distribute-image', 'install', 'keep', 'start', 'stop']

LIBRARY_PLACEMENT_STEP_KIND_VALUES: set[LibraryPlacementStepKind] = { 'build', 'create-placement', 'distribute-image', 'install', 'keep', 'start', 'stop',  }

def check_library_placement_step_kind(value: str) -> LibraryPlacementStepKind:
    if value in LIBRARY_PLACEMENT_STEP_KIND_VALUES:
        return cast(LibraryPlacementStepKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_PLACEMENT_STEP_KIND_VALUES!r}")
