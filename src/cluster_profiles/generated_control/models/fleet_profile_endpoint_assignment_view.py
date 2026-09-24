from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_endpoint_assignment_view_desired_state import check_fleet_profile_endpoint_assignment_view_desired_state
from ..models.fleet_profile_endpoint_assignment_view_desired_state import FleetProfileEndpointAssignmentViewDesiredState
from ..models.fleet_profile_endpoint_assignment_view_state import check_fleet_profile_endpoint_assignment_view_state
from ..models.fleet_profile_endpoint_assignment_view_state import FleetProfileEndpointAssignmentViewState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.endpoint_response import EndpointResponse





T = TypeVar("T", bound="FleetProfileEndpointAssignmentView")



@_attrs_define
class FleetProfileEndpointAssignmentView:
    """
        Attributes:
            assignment_id (str):
            desired_state (FleetProfileEndpointAssignmentViewDesiredState):
            recipe_title (str):
            state (FleetProfileEndpointAssignmentViewState):
            alias (Union[None, Unset, str]):
            endpoint (Union['EndpointResponse', None, Unset]):
     """

    assignment_id: str
    desired_state: FleetProfileEndpointAssignmentViewDesiredState
    recipe_title: str
    state: FleetProfileEndpointAssignmentViewState
    alias: Union[None, Unset, str] = UNSET
    endpoint: Union['EndpointResponse', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.endpoint_response import EndpointResponse
        assignment_id = self.assignment_id

        desired_state: str = self.desired_state

        recipe_title = self.recipe_title

        state: str = self.state

        alias: Union[None, Unset, str]
        if isinstance(self.alias, Unset):
            alias = UNSET
        else:
            alias = self.alias

        endpoint: Union[None, Unset, dict[str, Any]]
        if isinstance(self.endpoint, Unset):
            endpoint = UNSET
        elif isinstance(self.endpoint, EndpointResponse):
            endpoint = self.endpoint.to_dict()
        else:
            endpoint = self.endpoint


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assignment_id": assignment_id,
            "desired_state": desired_state,
            "recipe_title": recipe_title,
            "state": state,
        })
        if alias is not UNSET:
            field_dict["alias"] = alias
        if endpoint is not UNSET:
            field_dict["endpoint"] = endpoint

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.endpoint_response import EndpointResponse
        d = dict(src_dict)
        assignment_id = d.pop("assignment_id")

        desired_state = check_fleet_profile_endpoint_assignment_view_desired_state(d.pop("desired_state"))




        recipe_title = d.pop("recipe_title")

        state = check_fleet_profile_endpoint_assignment_view_state(d.pop("state"))




        def _parse_alias(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        alias = _parse_alias(d.pop("alias", UNSET))


        def _parse_endpoint(data: object) -> Union['EndpointResponse', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                endpoint_type_0 = EndpointResponse.from_dict(data)



                return endpoint_type_0
            except: # noqa: E722
                pass
            return cast(Union['EndpointResponse', None, Unset], data)

        endpoint = _parse_endpoint(d.pop("endpoint", UNSET))


        fleet_profile_endpoint_assignment_view = cls(
            assignment_id=assignment_id,
            desired_state=desired_state,
            recipe_title=recipe_title,
            state=state,
            alias=alias,
            endpoint=endpoint,
        )

        return fleet_profile_endpoint_assignment_view
