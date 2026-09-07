from typing import Literal, cast

FleetProfileAssignmentPreviewActionsItem = Literal['build', 'create-placement', 'distribute-image', 'install', 'keep', 'start', 'stop', 'switch']

FLEET_PROFILE_ASSIGNMENT_PREVIEW_ACTIONS_ITEM_VALUES: set[FleetProfileAssignmentPreviewActionsItem] = { 'build', 'create-placement', 'distribute-image', 'install', 'keep', 'start', 'stop', 'switch',  }

def check_fleet_profile_assignment_preview_actions_item(value: str) -> FleetProfileAssignmentPreviewActionsItem:
    if value in FLEET_PROFILE_ASSIGNMENT_PREVIEW_ACTIONS_ITEM_VALUES:
        return cast(FleetProfileAssignmentPreviewActionsItem, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {FLEET_PROFILE_ASSIGNMENT_PREVIEW_ACTIONS_ITEM_VALUES!r}")
