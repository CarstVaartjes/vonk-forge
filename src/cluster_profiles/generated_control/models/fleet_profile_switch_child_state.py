from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_switch_child_state_kind import check_fleet_profile_switch_child_state_kind
from ..models.fleet_profile_switch_child_state_kind import FleetProfileSwitchChildStateKind
from ..models.fleet_profile_switch_child_state_state import check_fleet_profile_switch_child_state_state
from ..models.fleet_profile_switch_child_state_state import FleetProfileSwitchChildStateState
from typing import cast






T = TypeVar("T", bound="FleetProfileSwitchChildState")



@_attrs_define
class FleetProfileSwitchChildState:
    """ Terminal receipt for a child already completed by the adapter.

        Attributes:
            kind (FleetProfileSwitchChildStateKind):
            operation_id (str):
            state (FleetProfileSwitchChildStateState):
     """

    kind: FleetProfileSwitchChildStateKind
    operation_id: str
    state: FleetProfileSwitchChildStateState





    def to_dict(self) -> dict[str, Any]:
        kind: str = self.kind

        operation_id = self.operation_id

        state: str = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "kind": kind,
            "operation_id": operation_id,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = check_fleet_profile_switch_child_state_kind(d.pop("kind"))




        operation_id = d.pop("operation_id")

        state = check_fleet_profile_switch_child_state_state(d.pop("state"))




        fleet_profile_switch_child_state = cls(
            kind=kind,
            operation_id=operation_id,
            state=state,
        )

        return fleet_profile_switch_child_state
