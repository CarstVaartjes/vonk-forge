from typing import Literal, cast

MappingSelectionAction = Literal['create', 'reuse']

MAPPING_SELECTION_ACTION_VALUES: set[MappingSelectionAction] = { 'create', 'reuse',  }

def check_mapping_selection_action(value: str) -> MappingSelectionAction:
    if value in MAPPING_SELECTION_ACTION_VALUES:
        return cast(MappingSelectionAction, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {MAPPING_SELECTION_ACTION_VALUES!r}")
