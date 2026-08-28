from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_assignment_preview_actions_item import check_fleet_profile_assignment_preview_actions_item
from ..models.fleet_profile_assignment_preview_actions_item import FleetProfileAssignmentPreviewActionsItem
from ..models.fleet_profile_assignment_preview_current_state import check_fleet_profile_assignment_preview_current_state
from ..models.fleet_profile_assignment_preview_current_state import FleetProfileAssignmentPreviewCurrentState
from ..models.fleet_profile_assignment_preview_desired_state import check_fleet_profile_assignment_preview_desired_state
from ..models.fleet_profile_assignment_preview_desired_state import FleetProfileAssignmentPreviewDesiredState
from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_reason import FleetProfileReason





T = TypeVar("T", bound="FleetProfileAssignmentPreview")



@_attrs_define
class FleetProfileAssignmentPreview:
    """
        Attributes:
            actions (list[FleetProfileAssignmentPreviewActionsItem]):
            assignment_id (str):
            current_state (FleetProfileAssignmentPreviewCurrentState):
            desired_state (FleetProfileAssignmentPreviewDesiredState):
            node_ids (list[str]):
            reasons (list['FleetProfileReason']):
            recipe_revision_id (str):
            recipe_title (str):
     """

    actions: list[FleetProfileAssignmentPreviewActionsItem]
    assignment_id: str
    current_state: FleetProfileAssignmentPreviewCurrentState
    desired_state: FleetProfileAssignmentPreviewDesiredState
    node_ids: list[str]
    reasons: list['FleetProfileReason']
    recipe_revision_id: str
    recipe_title: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_reason import FleetProfileReason
        actions = []
        for actions_item_data in self.actions:
            actions_item: str = actions_item_data
            actions.append(actions_item)



        assignment_id = self.assignment_id

        current_state: str = self.current_state

        desired_state: str = self.desired_state

        node_ids = self.node_ids



        reasons = []
        for reasons_item_data in self.reasons:
            reasons_item = reasons_item_data.to_dict()
            reasons.append(reasons_item)



        recipe_revision_id = self.recipe_revision_id

        recipe_title = self.recipe_title


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "actions": actions,
            "assignment_id": assignment_id,
            "current_state": current_state,
            "desired_state": desired_state,
            "node_ids": node_ids,
            "reasons": reasons,
            "recipe_revision_id": recipe_revision_id,
            "recipe_title": recipe_title,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_reason import FleetProfileReason
        d = dict(src_dict)
        actions = []
        _actions = d.pop("actions")
        for actions_item_data in (_actions):
            actions_item = check_fleet_profile_assignment_preview_actions_item(actions_item_data)



            actions.append(actions_item)


        assignment_id = d.pop("assignment_id")

        current_state = check_fleet_profile_assignment_preview_current_state(d.pop("current_state"))




        desired_state = check_fleet_profile_assignment_preview_desired_state(d.pop("desired_state"))




        node_ids = cast(list[str], d.pop("node_ids"))


        reasons = []
        _reasons = d.pop("reasons")
        for reasons_item_data in (_reasons):
            reasons_item = FleetProfileReason.from_dict(reasons_item_data)



            reasons.append(reasons_item)


        recipe_revision_id = d.pop("recipe_revision_id")

        recipe_title = d.pop("recipe_title")

        fleet_profile_assignment_preview = cls(
            actions=actions,
            assignment_id=assignment_id,
            current_state=current_state,
            desired_state=desired_state,
            node_ids=node_ids,
            reasons=reasons,
            recipe_revision_id=recipe_revision_id,
            recipe_title=recipe_title,
        )

        return fleet_profile_assignment_preview
