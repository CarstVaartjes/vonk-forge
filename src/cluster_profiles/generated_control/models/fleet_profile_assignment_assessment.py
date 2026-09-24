from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.run_switch_assessment import RunSwitchAssessment





T = TypeVar("T", bound="FleetProfileAssignmentAssessment")



@_attrs_define
class FleetProfileAssignmentAssessment:
    """
        Attributes:
            assessment (RunSwitchAssessment): Planner-owned admission and observations shared by operator reviews.
            assignment_id (str):
     """

    assessment: 'RunSwitchAssessment'
    assignment_id: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_assessment import RunSwitchAssessment
        assessment = self.assessment.to_dict()

        assignment_id = self.assignment_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assessment": assessment,
            "assignment_id": assignment_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_assessment import RunSwitchAssessment
        d = dict(src_dict)
        assessment = RunSwitchAssessment.from_dict(d.pop("assessment"))




        assignment_id = d.pop("assignment_id")

        fleet_profile_assignment_assessment = cls(
            assessment=assessment,
            assignment_id=assignment_id,
        )

        return fleet_profile_assignment_assessment
