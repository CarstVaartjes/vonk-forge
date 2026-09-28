from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_endpoint_assignment_view_desired_state import check_fleet_profile_endpoint_assignment_view_desired_state
from ..models.fleet_profile_endpoint_assignment_view_desired_state import FleetProfileEndpointAssignmentViewDesiredState
from ..models.fleet_profile_endpoint_assignment_view_state import check_fleet_profile_endpoint_assignment_view_state
from ..models.fleet_profile_endpoint_assignment_view_state import FleetProfileEndpointAssignmentViewState
from ..types import UNSET, Unset
from typing import cast

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
            alias (None | str | Unset):
            endpoint (EndpointResponse | None | Unset):
     """

    assignment_id: str
    desired_state: FleetProfileEndpointAssignmentViewDesiredState
    recipe_title: str
    state: FleetProfileEndpointAssignmentViewState
    alias: None | str | Unset = UNSET
    endpoint: EndpointResponse | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.endpoint_response import EndpointResponse # noqa: PLC0415
        assignment_id = self.assignment_id

        desired_state: str = self.desired_state

        recipe_title = self.recipe_title

        state: str = self.state

        alias: None | str | Unset
        if isinstance(self.alias, Unset):
            alias = UNSET
        else:
            alias = self.alias

        endpoint: dict[str, Any] | None | Unset
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
        from ..models.endpoint_response import EndpointResponse # noqa: PLC0415
        d = dict(src_dict)
        assignment_id = d.pop("assignment_id")

        desired_state = check_fleet_profile_endpoint_assignment_view_desired_state(d.pop("desired_state"))




        recipe_title = d.pop("recipe_title")

        state = check_fleet_profile_endpoint_assignment_view_state(d.pop("state"))




        def _parse_alias(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        alias = _parse_alias(d.pop("alias", UNSET))


        def _parse_endpoint(data: object) -> EndpointResponse | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                endpoint_type_0 = EndpointResponse.from_dict(data)



                return endpoint_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EndpointResponse | None | Unset, data)

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
