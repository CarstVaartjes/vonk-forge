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






T = TypeVar("T", bound="FleetProfileAssignmentInput")



@_attrs_define
class FleetProfileAssignmentInput:
    """ Permissive autosaved recipe choice, not an execution assignment.

    A logical model variant is selected by its canonical identity.  The
    content digest is resolved from that identity for a load and is never a
    profile-pinned execution revision.  Topology/rank validation belongs to
    preview/load, so incomplete distributed drafts can be saved.

        Attributes:
            recipe_selector (str):
            spark_ids (list[str]):
            assignment_name (Union[None, Unset, str]):
            desired_state (Union[Unset, FleetProfileAssignmentInputDesiredState]):  Default: 'running'.
            model_variant (Union[None, Unset, str]):
     """

    recipe_selector: str
    spark_ids: list[str]
    assignment_name: Union[None, Unset, str] = UNSET
    desired_state: Union[Unset, FleetProfileAssignmentInputDesiredState] = 'running'
    model_variant: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        recipe_selector = self.recipe_selector

        spark_ids = self.spark_ids



        assignment_name: Union[None, Unset, str]
        if isinstance(self.assignment_name, Unset):
            assignment_name = UNSET
        else:
            assignment_name = self.assignment_name

        desired_state: Union[Unset, str] = UNSET
        if not isinstance(self.desired_state, Unset):
            desired_state = self.desired_state


        model_variant: Union[None, Unset, str]
        if isinstance(self.model_variant, Unset):
            model_variant = UNSET
        else:
            model_variant = self.model_variant


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "recipe_selector": recipe_selector,
            "spark_ids": spark_ids,
        })
        if assignment_name is not UNSET:
            field_dict["assignment_name"] = assignment_name
        if desired_state is not UNSET:
            field_dict["desired_state"] = desired_state
        if model_variant is not UNSET:
            field_dict["model_variant"] = model_variant

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        recipe_selector = d.pop("recipe_selector")

        spark_ids = cast(list[str], d.pop("spark_ids"))


        def _parse_assignment_name(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        assignment_name = _parse_assignment_name(d.pop("assignment_name", UNSET))


        _desired_state = d.pop("desired_state", UNSET)
        desired_state: Union[Unset, FleetProfileAssignmentInputDesiredState]
        if isinstance(_desired_state,  Unset):
            desired_state = UNSET
        else:
            desired_state = check_fleet_profile_assignment_input_desired_state(_desired_state)




        def _parse_model_variant(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        model_variant = _parse_model_variant(d.pop("model_variant", UNSET))


        fleet_profile_assignment_input = cls(
            recipe_selector=recipe_selector,
            spark_ids=spark_ids,
            assignment_name=assignment_name,
            desired_state=desired_state,
            model_variant=model_variant,
        )

        return fleet_profile_assignment_input
