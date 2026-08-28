from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_plan_step_kind import check_fleet_profile_plan_step_kind
from ..models.fleet_profile_plan_step_kind import FleetProfilePlanStepKind
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="FleetProfilePlanStep")



@_attrs_define
class FleetProfilePlanStep:
    """
        Attributes:
            index (int):
            kind (FleetProfilePlanStepKind):
            label (str):
            assignment_id (Union[None, Unset, str]):
            node_ids (Union[Unset, list[str]]):
            owner_id (Union[None, Unset, str]):
            recipe_revision_id (Union[None, Unset, str]):
     """

    index: int
    kind: FleetProfilePlanStepKind
    label: str
    assignment_id: Union[None, Unset, str] = UNSET
    node_ids: Union[Unset, list[str]] = UNSET
    owner_id: Union[None, Unset, str] = UNSET
    recipe_revision_id: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        index = self.index

        kind: str = self.kind

        label = self.label

        assignment_id: Union[None, Unset, str]
        if isinstance(self.assignment_id, Unset):
            assignment_id = UNSET
        else:
            assignment_id = self.assignment_id

        node_ids: Union[Unset, list[str]] = UNSET
        if not isinstance(self.node_ids, Unset):
            node_ids = self.node_ids



        owner_id: Union[None, Unset, str]
        if isinstance(self.owner_id, Unset):
            owner_id = UNSET
        else:
            owner_id = self.owner_id

        recipe_revision_id: Union[None, Unset, str]
        if isinstance(self.recipe_revision_id, Unset):
            recipe_revision_id = UNSET
        else:
            recipe_revision_id = self.recipe_revision_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "index": index,
            "kind": kind,
            "label": label,
        })
        if assignment_id is not UNSET:
            field_dict["assignment_id"] = assignment_id
        if node_ids is not UNSET:
            field_dict["node_ids"] = node_ids
        if owner_id is not UNSET:
            field_dict["owner_id"] = owner_id
        if recipe_revision_id is not UNSET:
            field_dict["recipe_revision_id"] = recipe_revision_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        index = d.pop("index")

        kind = check_fleet_profile_plan_step_kind(d.pop("kind"))




        label = d.pop("label")

        def _parse_assignment_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        assignment_id = _parse_assignment_id(d.pop("assignment_id", UNSET))


        node_ids = cast(list[str], d.pop("node_ids", UNSET))


        def _parse_owner_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        owner_id = _parse_owner_id(d.pop("owner_id", UNSET))


        def _parse_recipe_revision_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        recipe_revision_id = _parse_recipe_revision_id(d.pop("recipe_revision_id", UNSET))


        fleet_profile_plan_step = cls(
            index=index,
            kind=kind,
            label=label,
            assignment_id=assignment_id,
            node_ids=node_ids,
            owner_id=owner_id,
            recipe_revision_id=recipe_revision_id,
        )

        return fleet_profile_plan_step
