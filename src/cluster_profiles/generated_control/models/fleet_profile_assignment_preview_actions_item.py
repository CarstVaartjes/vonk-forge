from typing import Literal, cast

FleetProfileAssignmentPreviewActionsItem = Literal['create-placement', 'distribute-image', 'install', 'keep', 'start', 'stop']

FLEET_PROFILE_ASSIGNMENT_PREVIEW_ACTIONS_ITEM_VALUES: set[FleetProfileAssignmentPreviewActionsItem] = { 'create-placement', 'distribute-image', 'install', 'keep', 'start', 'stop',  }

def check_fleet_profile_assignment_preview_actions_item(value: str) -> FleetProfileAssignmentPreviewActionsItem:
    if value in FLEET_PROFILE_ASSIGNMENT_PREVIEW_ACTIONS_ITEM_VALUES:
        return cast(FleetProfileAssignmentPreviewActionsItem, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_ASSIGNMENT_PREVIEW_ACTIONS_ITEM_VALUES!r}")
