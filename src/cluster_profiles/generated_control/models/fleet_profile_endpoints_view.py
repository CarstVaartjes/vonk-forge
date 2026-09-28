from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_endpoints_view_application_state_type_0 import check_fleet_profile_endpoints_view_application_state_type_0
from ..models.fleet_profile_endpoints_view_application_state_type_0 import FleetProfileEndpointsViewApplicationStateType0
from ..types import UNSET, Unset
from typing import cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_endpoint_assignment_view import FleetProfileEndpointAssignmentView
  from ..models.fleet_profile_endpoint_projection_issue import FleetProfileEndpointProjectionIssue





T = TypeVar("T", bound="FleetProfileEndpointsView")



@_attrs_define
class FleetProfileEndpointsView:
    """
        Attributes:
            assignments (list[FleetProfileEndpointAssignmentView] | None):
            number (int):
            observed_at (datetime.datetime):
            application_id (None | str | Unset):
            application_state (FleetProfileEndpointsViewApplicationStateType0 | None | Unset):
            profile_id (None | str | Unset):
            projection_issue (FleetProfileEndpointProjectionIssue | None | Unset):
     """

    assignments: list[FleetProfileEndpointAssignmentView] | None
    number: int
    observed_at: datetime.datetime
    application_id: None | str | Unset = UNSET
    application_state: FleetProfileEndpointsViewApplicationStateType0 | None | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    projection_issue: FleetProfileEndpointProjectionIssue | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_endpoint_assignment_view import FleetProfileEndpointAssignmentView # noqa: PLC0415
        from ..models.fleet_profile_endpoint_projection_issue import FleetProfileEndpointProjectionIssue # noqa: PLC0415
        assignments: list[dict[str, Any]] | None
        if isinstance(self.assignments, list):
            assignments = []
            for assignments_type_0_item_data in self.assignments:
                assignments_type_0_item = assignments_type_0_item_data.to_dict()
                assignments.append(assignments_type_0_item)


        else:
            assignments = self.assignments

        number = self.number

        observed_at = self.observed_at.isoformat()

        application_id: None | str | Unset
        if isinstance(self.application_id, Unset):
            application_id = UNSET
        else:
            application_id = self.application_id

        application_state: None | str | Unset
        if isinstance(self.application_state, Unset):
            application_state = UNSET
        elif isinstance(self.application_state, str):
            application_state = self.application_state
        else:
            application_state = self.application_state

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        projection_issue: dict[str, Any] | None | Unset
        if isinstance(self.projection_issue, Unset):
            projection_issue = UNSET
        elif isinstance(self.projection_issue, FleetProfileEndpointProjectionIssue):
            projection_issue = self.projection_issue.to_dict()
        else:
            projection_issue = self.projection_issue


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assignments": assignments,
            "number": number,
            "observed_at": observed_at,
        })
        if application_id is not UNSET:
            field_dict["application_id"] = application_id
        if application_state is not UNSET:
            field_dict["application_state"] = application_state
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if projection_issue is not UNSET:
            field_dict["projection_issue"] = projection_issue

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_endpoint_assignment_view import FleetProfileEndpointAssignmentView # noqa: PLC0415
        from ..models.fleet_profile_endpoint_projection_issue import FleetProfileEndpointProjectionIssue # noqa: PLC0415
        d = dict(src_dict)
        def _parse_assignments(data: object) -> list[FleetProfileEndpointAssignmentView] | None:
            if data is None:
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                assignments_type_0 = []
                _assignments_type_0 = data
                for assignments_type_0_item_data in (_assignments_type_0):
                    assignments_type_0_item = FleetProfileEndpointAssignmentView.from_dict(assignments_type_0_item_data)



                    assignments_type_0.append(assignments_type_0_item)

                return assignments_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[FleetProfileEndpointAssignmentView] | None, data)

        assignments = _parse_assignments(d.pop("assignments"))


        number = d.pop("number")

        observed_at = datetime.datetime.fromisoformat(d.pop("observed_at"))




        def _parse_application_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        application_id = _parse_application_id(d.pop("application_id", UNSET))


        def _parse_application_state(data: object) -> FleetProfileEndpointsViewApplicationStateType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                application_state_type_0 = check_fleet_profile_endpoints_view_application_state_type_0(data)



                return application_state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileEndpointsViewApplicationStateType0 | None | Unset, data)

        application_state = _parse_application_state(d.pop("application_state", UNSET))


        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))


        def _parse_projection_issue(data: object) -> FleetProfileEndpointProjectionIssue | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                projection_issue_type_0 = FleetProfileEndpointProjectionIssue.from_dict(data)



                return projection_issue_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileEndpointProjectionIssue | None | Unset, data)

        projection_issue = _parse_projection_issue(d.pop("projection_issue", UNSET))


        fleet_profile_endpoints_view = cls(
            assignments=assignments,
            number=number,
            observed_at=observed_at,
            application_id=application_id,
            application_state=application_state,
            profile_id=profile_id,
            projection_issue=projection_issue,
        )

        return fleet_profile_endpoints_view
