from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_switch_child_state import FleetProfileSwitchChildState





T = TypeVar("T", bound="FleetProfileSwitchAdapterResult")



@_attrs_define
class FleetProfileSwitchAdapterResult:
    """ Complete result of reconciling every assignment in a profile scope.

        Attributes:
            assignment_ids (list[str]):
            children (list['FleetProfileSwitchChildState']):
     """

    assignment_ids: list[str]
    children: list['FleetProfileSwitchChildState']





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_switch_child_state import FleetProfileSwitchChildState
        assignment_ids = self.assignment_ids



        children = []
        for children_item_data in self.children:
            children_item = children_item_data.to_dict()
            children.append(children_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assignment_ids": assignment_ids,
            "children": children,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_switch_child_state import FleetProfileSwitchChildState
        d = dict(src_dict)
        assignment_ids = cast(list[str], d.pop("assignment_ids"))


        children = []
        _children = d.pop("children")
        for children_item_data in (_children):
            children_item = FleetProfileSwitchChildState.from_dict(children_item_data)



            children.append(children_item)


        fleet_profile_switch_adapter_result = cls(
            assignment_ids=assignment_ids,
            children=children,
        )

        return fleet_profile_switch_adapter_result
