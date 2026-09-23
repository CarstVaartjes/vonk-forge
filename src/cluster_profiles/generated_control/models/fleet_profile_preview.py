from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_scope_preview import FleetProfileScopePreview
  from ..models.fleet_profile_definition import FleetProfileDefinition
  from ..models.fleet_profile_preparation_decision import FleetProfilePreparationDecision
  from ..models.fleet_profile_effects import FleetProfileEffects
  from ..models.fleet_profile_assignment_preview import FleetProfileAssignmentPreview
  from ..models.fleet_profile_admission_decision import FleetProfileAdmissionDecision
  from ..models.fleet_profile_assignment_assessment import FleetProfileAssignmentAssessment
  from ..models.fleet_profile_assignment_preparation import FleetProfileAssignmentPreparation
  from ..models.fleet_profile_plan_summary import FleetProfilePlanSummary
  from ..models.fleet_profile_plan_step import FleetProfilePlanStep
  from ..models.fleet_profile_assignment import FleetProfileAssignment
  from ..models.fleet_profile_reason import FleetProfileReason





T = TypeVar("T", bound="FleetProfilePreview")



@_attrs_define
class FleetProfilePreview:
    """
        Attributes:
            admission_decisions (list['FleetProfileAdmissionDecision']):
            allowed (bool):
            assessments (list['FleetProfileAssignmentAssessment']):
            assignments (list['FleetProfileAssignmentPreview']):
            effects (FleetProfileEffects): Identified live effects, including complete distributed membership.
            generated_at (datetime.datetime):
            plan_digest (str):
            preparation_decisions (list['FleetProfilePreparationDecision']):
            profile_definition (Union['FleetProfileDefinition', None]):
            profile_digest (str):
            profile_id (str):
            profile_name (str):
            profile_revision (Union[None, int]):
            reasons (list['FleetProfileReason']):
            resolved_assignments (list['FleetProfileAssignment']):
            scope (FleetProfileScopePreview):
            steps (list['FleetProfilePlanStep']):
            summary (FleetProfilePlanSummary):
            preparations (Union[Unset, list['FleetProfileAssignmentPreparation']]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    admission_decisions: list['FleetProfileAdmissionDecision']
    allowed: bool
    assessments: list['FleetProfileAssignmentAssessment']
    assignments: list['FleetProfileAssignmentPreview']
    effects: 'FleetProfileEffects'
    generated_at: datetime.datetime
    plan_digest: str
    preparation_decisions: list['FleetProfilePreparationDecision']
    profile_definition: Union['FleetProfileDefinition', None]
    profile_digest: str
    profile_id: str
    profile_name: str
    profile_revision: Union[None, int]
    reasons: list['FleetProfileReason']
    resolved_assignments: list['FleetProfileAssignment']
    scope: 'FleetProfileScopePreview'
    steps: list['FleetProfilePlanStep']
    summary: 'FleetProfilePlanSummary'
    preparations: Union[Unset, list['FleetProfileAssignmentPreparation']] = UNSET
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_scope_preview import FleetProfileScopePreview
        from ..models.fleet_profile_definition import FleetProfileDefinition
        from ..models.fleet_profile_preparation_decision import FleetProfilePreparationDecision
        from ..models.fleet_profile_effects import FleetProfileEffects
        from ..models.fleet_profile_assignment_preview import FleetProfileAssignmentPreview
        from ..models.fleet_profile_admission_decision import FleetProfileAdmissionDecision
        from ..models.fleet_profile_assignment_assessment import FleetProfileAssignmentAssessment
        from ..models.fleet_profile_assignment_preparation import FleetProfileAssignmentPreparation
        from ..models.fleet_profile_plan_summary import FleetProfilePlanSummary
        from ..models.fleet_profile_plan_step import FleetProfilePlanStep
        from ..models.fleet_profile_assignment import FleetProfileAssignment
        from ..models.fleet_profile_reason import FleetProfileReason
        admission_decisions = []
        for admission_decisions_item_data in self.admission_decisions:
            admission_decisions_item = admission_decisions_item_data.to_dict()
            admission_decisions.append(admission_decisions_item)



        allowed = self.allowed

        assessments = []
        for assessments_item_data in self.assessments:
            assessments_item = assessments_item_data.to_dict()
            assessments.append(assessments_item)



        assignments = []
        for assignments_item_data in self.assignments:
            assignments_item = assignments_item_data.to_dict()
            assignments.append(assignments_item)



        effects = self.effects.to_dict()

        generated_at = self.generated_at.isoformat()

        plan_digest = self.plan_digest

        preparation_decisions = []
        for preparation_decisions_item_data in self.preparation_decisions:
            preparation_decisions_item = preparation_decisions_item_data.to_dict()
            preparation_decisions.append(preparation_decisions_item)



        profile_definition: Union[None, dict[str, Any]]
        if isinstance(self.profile_definition, FleetProfileDefinition):
            profile_definition = self.profile_definition.to_dict()
        else:
            profile_definition = self.profile_definition

        profile_digest = self.profile_digest

        profile_id = self.profile_id

        profile_name = self.profile_name

        profile_revision: Union[None, int]
        profile_revision = self.profile_revision

        reasons = []
        for reasons_item_data in self.reasons:
            reasons_item = reasons_item_data.to_dict()
            reasons.append(reasons_item)



        resolved_assignments = []
        for resolved_assignments_item_data in self.resolved_assignments:
            resolved_assignments_item = resolved_assignments_item_data.to_dict()
            resolved_assignments.append(resolved_assignments_item)



        scope = self.scope.to_dict()

        steps = []
        for steps_item_data in self.steps:
            steps_item = steps_item_data.to_dict()
            steps.append(steps_item)



        summary = self.summary.to_dict()

        preparations: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.preparations, Unset):
            preparations = []
            for preparations_item_data in self.preparations:
                preparations_item = preparations_item_data.to_dict()
                preparations.append(preparations_item)



        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "admission_decisions": admission_decisions,
            "allowed": allowed,
            "assessments": assessments,
            "assignments": assignments,
            "effects": effects,
            "generated_at": generated_at,
            "plan_digest": plan_digest,
            "preparation_decisions": preparation_decisions,
            "profile_definition": profile_definition,
            "profile_digest": profile_digest,
            "profile_id": profile_id,
            "profile_name": profile_name,
            "profile_revision": profile_revision,
            "reasons": reasons,
            "resolved_assignments": resolved_assignments,
            "scope": scope,
            "steps": steps,
            "summary": summary,
        })
        if preparations is not UNSET:
            field_dict["preparations"] = preparations
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_scope_preview import FleetProfileScopePreview
        from ..models.fleet_profile_definition import FleetProfileDefinition
        from ..models.fleet_profile_preparation_decision import FleetProfilePreparationDecision
        from ..models.fleet_profile_effects import FleetProfileEffects
        from ..models.fleet_profile_assignment_preview import FleetProfileAssignmentPreview
        from ..models.fleet_profile_admission_decision import FleetProfileAdmissionDecision
        from ..models.fleet_profile_assignment_assessment import FleetProfileAssignmentAssessment
        from ..models.fleet_profile_assignment_preparation import FleetProfileAssignmentPreparation
        from ..models.fleet_profile_plan_summary import FleetProfilePlanSummary
        from ..models.fleet_profile_plan_step import FleetProfilePlanStep
        from ..models.fleet_profile_assignment import FleetProfileAssignment
        from ..models.fleet_profile_reason import FleetProfileReason
        d = dict(src_dict)
        admission_decisions = []
        _admission_decisions = d.pop("admission_decisions")
        for admission_decisions_item_data in (_admission_decisions):
            admission_decisions_item = FleetProfileAdmissionDecision.from_dict(admission_decisions_item_data)



            admission_decisions.append(admission_decisions_item)


        allowed = d.pop("allowed")

        assessments = []
        _assessments = d.pop("assessments")
        for assessments_item_data in (_assessments):
            assessments_item = FleetProfileAssignmentAssessment.from_dict(assessments_item_data)



            assessments.append(assessments_item)


        assignments = []
        _assignments = d.pop("assignments")
        for assignments_item_data in (_assignments):
            assignments_item = FleetProfileAssignmentPreview.from_dict(assignments_item_data)



            assignments.append(assignments_item)


        effects = FleetProfileEffects.from_dict(d.pop("effects"))




        generated_at = isoparse(d.pop("generated_at"))




        plan_digest = d.pop("plan_digest")

        preparation_decisions = []
        _preparation_decisions = d.pop("preparation_decisions")
        for preparation_decisions_item_data in (_preparation_decisions):
            preparation_decisions_item = FleetProfilePreparationDecision.from_dict(preparation_decisions_item_data)



            preparation_decisions.append(preparation_decisions_item)


        def _parse_profile_definition(data: object) -> Union['FleetProfileDefinition', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                profile_definition_type_0 = FleetProfileDefinition.from_dict(data)



                return profile_definition_type_0
            except: # noqa: E722
                pass
            return cast(Union['FleetProfileDefinition', None], data)

        profile_definition = _parse_profile_definition(d.pop("profile_definition"))


        profile_digest = d.pop("profile_digest")

        profile_id = d.pop("profile_id")

        profile_name = d.pop("profile_name")

        def _parse_profile_revision(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        profile_revision = _parse_profile_revision(d.pop("profile_revision"))


        reasons = []
        _reasons = d.pop("reasons")
        for reasons_item_data in (_reasons):
            reasons_item = FleetProfileReason.from_dict(reasons_item_data)



            reasons.append(reasons_item)


        resolved_assignments = []
        _resolved_assignments = d.pop("resolved_assignments")
        for resolved_assignments_item_data in (_resolved_assignments):
            resolved_assignments_item = FleetProfileAssignment.from_dict(resolved_assignments_item_data)



            resolved_assignments.append(resolved_assignments_item)


        scope = FleetProfileScopePreview.from_dict(d.pop("scope"))




        steps = []
        _steps = d.pop("steps")
        for steps_item_data in (_steps):
            steps_item = FleetProfilePlanStep.from_dict(steps_item_data)



            steps.append(steps_item)


        summary = FleetProfilePlanSummary.from_dict(d.pop("summary"))




        preparations = []
        _preparations = d.pop("preparations", UNSET)
        for preparations_item_data in (_preparations or []):
            preparations_item = FleetProfileAssignmentPreparation.from_dict(preparations_item_data)



            preparations.append(preparations_item)


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        fleet_profile_preview = cls(
            admission_decisions=admission_decisions,
            allowed=allowed,
            assessments=assessments,
            assignments=assignments,
            effects=effects,
            generated_at=generated_at,
            plan_digest=plan_digest,
            preparation_decisions=preparation_decisions,
            profile_definition=profile_definition,
            profile_digest=profile_digest,
            profile_id=profile_id,
            profile_name=profile_name,
            profile_revision=profile_revision,
            reasons=reasons,
            resolved_assignments=resolved_assignments,
            scope=scope,
            steps=steps,
            summary=summary,
            preparations=preparations,
            schema_version=schema_version,
        )

        return fleet_profile_preview
