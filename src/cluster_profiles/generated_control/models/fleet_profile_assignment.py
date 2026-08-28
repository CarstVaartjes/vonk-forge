from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_assignment_desired_state import check_fleet_profile_assignment_desired_state
from ..models.fleet_profile_assignment_desired_state import FleetProfileAssignmentDesiredState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.fleet_profile_node import FleetProfileNode





T = TypeVar("T", bound="FleetProfileAssignment")



@_attrs_define
class FleetProfileAssignment:
    """
        Attributes:
            desired_state (FleetProfileAssignmentDesiredState):
            id (str):
            nodes (list['FleetProfileNode']):
            recipe_id (str):
            recipe_revision_id (str):
            recipe_title (str):
            topology_name (str):
            alias (Union[None, Unset, str]):
            model_title (Union[None, Unset, str]):
     """

    desired_state: FleetProfileAssignmentDesiredState
    id: str
    nodes: list['FleetProfileNode']
    recipe_id: str
    recipe_revision_id: str
    recipe_title: str
    topology_name: str
    alias: Union[None, Unset, str] = UNSET
    model_title: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_node import FleetProfileNode
        desired_state: str = self.desired_state

        id = self.id

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        recipe_id = self.recipe_id

        recipe_revision_id = self.recipe_revision_id

        recipe_title = self.recipe_title

        topology_name = self.topology_name

        alias: Union[None, Unset, str]
        if isinstance(self.alias, Unset):
            alias = UNSET
        else:
            alias = self.alias

        model_title: Union[None, Unset, str]
        if isinstance(self.model_title, Unset):
            model_title = UNSET
        else:
            model_title = self.model_title


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "desired_state": desired_state,
            "id": id,
            "nodes": nodes,
            "recipe_id": recipe_id,
            "recipe_revision_id": recipe_revision_id,
            "recipe_title": recipe_title,
            "topology_name": topology_name,
        })
        if alias is not UNSET:
            field_dict["alias"] = alias
        if model_title is not UNSET:
            field_dict["model_title"] = model_title

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_node import FleetProfileNode
        d = dict(src_dict)
        desired_state = check_fleet_profile_assignment_desired_state(d.pop("desired_state"))




        id = d.pop("id")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = FleetProfileNode.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        recipe_id = d.pop("recipe_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        recipe_title = d.pop("recipe_title")

        topology_name = d.pop("topology_name")

        def _parse_alias(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        alias = _parse_alias(d.pop("alias", UNSET))


        def _parse_model_title(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        model_title = _parse_model_title(d.pop("model_title", UNSET))


        fleet_profile_assignment = cls(
            desired_state=desired_state,
            id=id,
            nodes=nodes,
            recipe_id=recipe_id,
            recipe_revision_id=recipe_revision_id,
            recipe_title=recipe_title,
            topology_name=topology_name,
            alias=alias,
            model_title=model_title,
        )

        return fleet_profile_assignment
