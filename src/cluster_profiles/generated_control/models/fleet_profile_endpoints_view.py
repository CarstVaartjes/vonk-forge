from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_endpoints_view_application_state_type_0 import check_fleet_profile_endpoints_view_application_state_type_0
from ..models.fleet_profile_endpoints_view_application_state_type_0 import FleetProfileEndpointsViewApplicationStateType0
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_endpoint_assignment_view import FleetProfileEndpointAssignmentView





T = TypeVar("T", bound="FleetProfileEndpointsView")



@_attrs_define
class FleetProfileEndpointsView:
    """
        Attributes:
            assignments (list['FleetProfileEndpointAssignmentView']):
            number (int):
            observed_at (datetime.datetime):
            application_id (Union[None, Unset, str]):
            application_state (Union[FleetProfileEndpointsViewApplicationStateType0, None, Unset]):
            profile_id (Union[None, Unset, str]):
     """

    assignments: list['FleetProfileEndpointAssignmentView']
    number: int
    observed_at: datetime.datetime
    application_id: Union[None, Unset, str] = UNSET
    application_state: Union[FleetProfileEndpointsViewApplicationStateType0, None, Unset] = UNSET
    profile_id: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_endpoint_assignment_view import FleetProfileEndpointAssignmentView
        assignments = []
        for assignments_item_data in self.assignments:
            assignments_item = assignments_item_data.to_dict()
            assignments.append(assignments_item)



        number = self.number

        observed_at = self.observed_at.isoformat()

        application_id: Union[None, Unset, str]
        if isinstance(self.application_id, Unset):
            application_id = UNSET
        else:
            application_id = self.application_id

        application_state: Union[None, Unset, str]
        if isinstance(self.application_state, Unset):
            application_state = UNSET
        elif isinstance(self.application_state, str):
            application_state = self.application_state
        else:
            application_state = self.application_state

        profile_id: Union[None, Unset, str]
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id


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

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_endpoint_assignment_view import FleetProfileEndpointAssignmentView
        d = dict(src_dict)
        assignments = []
        _assignments = d.pop("assignments")
        for assignments_item_data in (_assignments):
            assignments_item = FleetProfileEndpointAssignmentView.from_dict(assignments_item_data)



            assignments.append(assignments_item)


        number = d.pop("number")

        observed_at = isoparse(d.pop("observed_at"))




        def _parse_application_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        application_id = _parse_application_id(d.pop("application_id", UNSET))


        def _parse_application_state(data: object) -> Union[FleetProfileEndpointsViewApplicationStateType0, None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                application_state_type_0 = check_fleet_profile_endpoints_view_application_state_type_0(data)



                return application_state_type_0
            except: # noqa: E722
                pass
            return cast(Union[FleetProfileEndpointsViewApplicationStateType0, None, Unset], data)

        application_state = _parse_application_state(d.pop("application_state", UNSET))


        def _parse_profile_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))


        fleet_profile_endpoints_view = cls(
            assignments=assignments,
            number=number,
            observed_at=observed_at,
            application_id=application_id,
            application_state=application_state,
            profile_id=profile_id,
        )

        return fleet_profile_endpoints_view
