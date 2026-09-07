from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.rollout_preparation import RolloutPreparation





T = TypeVar("T", bound="FleetProfileAssignmentPreparation")



@_attrs_define
class FleetProfileAssignmentPreparation:
    """
        Attributes:
            assignment_id (str):
            preparation (RolloutPreparation): Normalized preparation identity shared by profiles, Run, web and CLI.
     """

    assignment_id: str
    preparation: 'RolloutPreparation'





    def to_dict(self) -> dict[str, Any]:
        from ..models.rollout_preparation import RolloutPreparation
        assignment_id = self.assignment_id

        preparation = self.preparation.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assignment_id": assignment_id,
            "preparation": preparation,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.rollout_preparation import RolloutPreparation
        d = dict(src_dict)
        assignment_id = d.pop("assignment_id")

        preparation = RolloutPreparation.from_dict(d.pop("preparation"))




        fleet_profile_assignment_preparation = cls(
            assignment_id=assignment_id,
            preparation=preparation,
        )

        return fleet_profile_assignment_preparation
