from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import Literal, Union, cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_plan_summary import FleetProfilePlanSummary
  from ..models.fleet_profile_plan_step import FleetProfilePlanStep
  from ..models.fleet_profile_assignment_preview import FleetProfileAssignmentPreview
  from ..models.fleet_profile_reason import FleetProfileReason





T = TypeVar("T", bound="FleetProfilePreview")



@_attrs_define
class FleetProfilePreview:
    """
        Attributes:
            allowed (bool):
            assignments (list['FleetProfileAssignmentPreview']):
            generated_at (datetime.datetime):
            plan_digest (str):
            profile_digest (str):
            profile_id (str):
            profile_name (str):
            reasons (list['FleetProfileReason']):
            steps (list['FleetProfilePlanStep']):
            summary (FleetProfilePlanSummary):
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    allowed: bool
    assignments: list['FleetProfileAssignmentPreview']
    generated_at: datetime.datetime
    plan_digest: str
    profile_digest: str
    profile_id: str
    profile_name: str
    reasons: list['FleetProfileReason']
    steps: list['FleetProfilePlanStep']
    summary: 'FleetProfilePlanSummary'
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_plan_summary import FleetProfilePlanSummary
        from ..models.fleet_profile_plan_step import FleetProfilePlanStep
        from ..models.fleet_profile_assignment_preview import FleetProfileAssignmentPreview
        from ..models.fleet_profile_reason import FleetProfileReason
        allowed = self.allowed

        assignments = []
        for assignments_item_data in self.assignments:
            assignments_item = assignments_item_data.to_dict()
            assignments.append(assignments_item)



        generated_at = self.generated_at.isoformat()

        plan_digest = self.plan_digest

        profile_digest = self.profile_digest

        profile_id = self.profile_id

        profile_name = self.profile_name

        reasons = []
        for reasons_item_data in self.reasons:
            reasons_item = reasons_item_data.to_dict()
            reasons.append(reasons_item)



        steps = []
        for steps_item_data in self.steps:
            steps_item = steps_item_data.to_dict()
            steps.append(steps_item)



        summary = self.summary.to_dict()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "allowed": allowed,
            "assignments": assignments,
            "generated_at": generated_at,
            "plan_digest": plan_digest,
            "profile_digest": profile_digest,
            "profile_id": profile_id,
            "profile_name": profile_name,
            "reasons": reasons,
            "steps": steps,
            "summary": summary,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_plan_summary import FleetProfilePlanSummary
        from ..models.fleet_profile_plan_step import FleetProfilePlanStep
        from ..models.fleet_profile_assignment_preview import FleetProfileAssignmentPreview
        from ..models.fleet_profile_reason import FleetProfileReason
        d = dict(src_dict)
        allowed = d.pop("allowed")

        assignments = []
        _assignments = d.pop("assignments")
        for assignments_item_data in (_assignments):
            assignments_item = FleetProfileAssignmentPreview.from_dict(assignments_item_data)



            assignments.append(assignments_item)


        generated_at = isoparse(d.pop("generated_at"))




        plan_digest = d.pop("plan_digest")

        profile_digest = d.pop("profile_digest")

        profile_id = d.pop("profile_id")

        profile_name = d.pop("profile_name")

        reasons = []
        _reasons = d.pop("reasons")
        for reasons_item_data in (_reasons):
            reasons_item = FleetProfileReason.from_dict(reasons_item_data)



            reasons.append(reasons_item)


        steps = []
        _steps = d.pop("steps")
        for steps_item_data in (_steps):
            steps_item = FleetProfilePlanStep.from_dict(steps_item_data)



            steps.append(steps_item)


        summary = FleetProfilePlanSummary.from_dict(d.pop("summary"))




        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        fleet_profile_preview = cls(
            allowed=allowed,
            assignments=assignments,
            generated_at=generated_at,
            plan_digest=plan_digest,
            profile_digest=profile_digest,
            profile_id=profile_id,
            profile_name=profile_name,
            reasons=reasons,
            steps=steps,
            summary=summary,
            schema_version=schema_version,
        )

        return fleet_profile_preview
