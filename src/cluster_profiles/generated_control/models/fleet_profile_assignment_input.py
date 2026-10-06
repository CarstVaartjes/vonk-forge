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
  from ..models.fleet_profile_assignment_input_option_choices import FleetProfileAssignmentInputOptionChoices





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
            assignment_name (None | str | Unset):
            desired_state (DesiredAssignmentState | Unset): What a fleet-profile assignment is asked to become on its
                Sparks.
            model_variant (None | str | Unset):
            option_choices (FleetProfileAssignmentInputOptionChoices | Unset):
     """

    recipe_selector: str
    spark_ids: list[str]
    assignment_name: None | str | Unset = UNSET
    desired_state: DesiredAssignmentState | Unset = UNSET
    model_variant: None | str | Unset = UNSET
    option_choices: FleetProfileAssignmentInputOptionChoices | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_assignment_input_option_choices import FleetProfileAssignmentInputOptionChoices # noqa: PLC0415
        recipe_selector = self.recipe_selector

        spark_ids = self.spark_ids



        assignment_name: None | str | Unset
        if isinstance(self.assignment_name, Unset):
            assignment_name = UNSET
        else:
            assignment_name = self.assignment_name

        desired_state: str | Unset = UNSET
        if not isinstance(self.desired_state, Unset):
            desired_state = self.desired_state


        model_variant: None | str | Unset
        if isinstance(self.model_variant, Unset):
            model_variant = UNSET
        else:
            model_variant = self.model_variant

        option_choices: dict[str, Any] | Unset = UNSET
        if not isinstance(self.option_choices, Unset):
            option_choices = self.option_choices.to_dict()


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
        if option_choices is not UNSET:
            field_dict["option_choices"] = option_choices

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_assignment_input_option_choices import FleetProfileAssignmentInputOptionChoices # noqa: PLC0415
        d = dict(src_dict)
        recipe_selector = d.pop("recipe_selector")

        spark_ids = cast(list[str], d.pop("spark_ids"))


        def _parse_assignment_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        assignment_name = _parse_assignment_name(d.pop("assignment_name", UNSET))


        _desired_state = d.pop("desired_state", UNSET)
        desired_state: DesiredAssignmentState | Unset
        if isinstance(_desired_state,  Unset):
            desired_state = UNSET
        else:
            desired_state = check_desired_assignment_state(_desired_state)




        def _parse_model_variant(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model_variant = _parse_model_variant(d.pop("model_variant", UNSET))


        _option_choices = d.pop("option_choices", UNSET)
        option_choices: FleetProfileAssignmentInputOptionChoices | Unset
        if isinstance(_option_choices,  Unset):
            option_choices = UNSET
        else:
            option_choices = FleetProfileAssignmentInputOptionChoices.from_dict(_option_choices)




        fleet_profile_assignment_input = cls(
            recipe_selector=recipe_selector,
            spark_ids=spark_ids,
            assignment_name=assignment_name,
            desired_state=desired_state,
            model_variant=model_variant,
            option_choices=option_choices,
        )

        return fleet_profile_assignment_input
