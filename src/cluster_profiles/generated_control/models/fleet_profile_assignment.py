from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.desired_assignment_state import check_desired_assignment_state
from ..models.desired_assignment_state import DesiredAssignmentState
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_assignment_option_choices import FleetProfileAssignmentOptionChoices
  from ..models.fleet_profile_node import FleetProfileNode





T = TypeVar("T", bound="FleetProfileAssignment")



@_attrs_define
class FleetProfileAssignment:
    """
        Attributes:
            desired_state (DesiredAssignmentState): What a fleet-profile assignment is asked to become on its Sparks.
            id (str):
            nodes (list[FleetProfileNode]):
            recipe_id (str):
            recipe_revision_id (str):
            recipe_title (str):
            topology_name (str):
            alias (None | str | Unset):
            model_title (None | str | Unset):
            option_choices (FleetProfileAssignmentOptionChoices | Unset):
     """

    desired_state: DesiredAssignmentState
    id: str
    nodes: list[FleetProfileNode]
    recipe_id: str
    recipe_revision_id: str
    recipe_title: str
    topology_name: str
    alias: None | str | Unset = UNSET
    model_title: None | str | Unset = UNSET
    option_choices: FleetProfileAssignmentOptionChoices | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_assignment_option_choices import FleetProfileAssignmentOptionChoices # noqa: PLC0415
        from ..models.fleet_profile_node import FleetProfileNode # noqa: PLC0415
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

        alias: None | str | Unset
        if isinstance(self.alias, Unset):
            alias = UNSET
        else:
            alias = self.alias

        model_title: None | str | Unset
        if isinstance(self.model_title, Unset):
            model_title = UNSET
        else:
            model_title = self.model_title

        option_choices: dict[str, Any] | Unset = UNSET
        if not isinstance(self.option_choices, Unset):
            option_choices = self.option_choices.to_dict()


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
        if option_choices is not UNSET:
            field_dict["option_choices"] = option_choices

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_assignment_option_choices import FleetProfileAssignmentOptionChoices # noqa: PLC0415
        from ..models.fleet_profile_node import FleetProfileNode # noqa: PLC0415
        d = dict(src_dict)
        desired_state = check_desired_assignment_state(d.pop("desired_state"))




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

        def _parse_alias(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        alias = _parse_alias(d.pop("alias", UNSET))


        def _parse_model_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model_title = _parse_model_title(d.pop("model_title", UNSET))


        _option_choices = d.pop("option_choices", UNSET)
        option_choices: FleetProfileAssignmentOptionChoices | Unset
        if isinstance(_option_choices,  Unset):
            option_choices = UNSET
        else:
            option_choices = FleetProfileAssignmentOptionChoices.from_dict(_option_choices)




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
            option_choices=option_choices,
        )

        return fleet_profile_assignment
