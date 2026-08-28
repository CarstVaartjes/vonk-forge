from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_assignment_input_desired_state import check_fleet_profile_assignment_input_desired_state
from ..models.fleet_profile_assignment_input_desired_state import FleetProfileAssignmentInputDesiredState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.fleet_profile_node import FleetProfileNode





T = TypeVar("T", bound="FleetProfileAssignmentInput")



@_attrs_define
class FleetProfileAssignmentInput:
    """
        Attributes:
            desired_state (FleetProfileAssignmentInputDesiredState):
            nodes (list['FleetProfileNode']):
            recipe_revision_id (str):
            topology_name (str):
            alias (Union[None, Unset, str]):
     """

    desired_state: FleetProfileAssignmentInputDesiredState
    nodes: list['FleetProfileNode']
    recipe_revision_id: str
    topology_name: str
    alias: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_node import FleetProfileNode
        desired_state: str = self.desired_state

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        recipe_revision_id = self.recipe_revision_id

        topology_name = self.topology_name

        alias: Union[None, Unset, str]
        if isinstance(self.alias, Unset):
            alias = UNSET
        else:
            alias = self.alias


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "desired_state": desired_state,
            "nodes": nodes,
            "recipe_revision_id": recipe_revision_id,
            "topology_name": topology_name,
        })
        if alias is not UNSET:
            field_dict["alias"] = alias

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_node import FleetProfileNode
        d = dict(src_dict)
        desired_state = check_fleet_profile_assignment_input_desired_state(d.pop("desired_state"))




        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = FleetProfileNode.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        recipe_revision_id = d.pop("recipe_revision_id")

        topology_name = d.pop("topology_name")

        def _parse_alias(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        alias = _parse_alias(d.pop("alias", UNSET))


        fleet_profile_assignment_input = cls(
            desired_state=desired_state,
            nodes=nodes,
            recipe_revision_id=recipe_revision_id,
            topology_name=topology_name,
            alias=alias,
        )

        return fleet_profile_assignment_input
