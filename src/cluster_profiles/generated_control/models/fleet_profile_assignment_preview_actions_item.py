from typing import Literal

FleetProfileAssignmentPreviewActionsItem = Literal['adopt', 'keep', 'switch']

FLEET_PROFILE_ASSIGNMENT_PREVIEW_ACTIONS_ITEM_VALUES: set[FleetProfileAssignmentPreviewActionsItem] = { 'adopt', 'keep', 'switch',  }

def check_fleet_profile_assignment_preview_actions_item(value: str) -> FleetProfileAssignmentPreviewActionsItem:
    if value in FLEET_PROFILE_ASSIGNMENT_PREVIEW_ACTIONS_ITEM_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_ASSIGNMENT_PREVIEW_ACTIONS_ITEM_VALUES!r}")
