from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_switch_child_state_kind import check_fleet_profile_switch_child_state_kind
from ..models.fleet_profile_switch_child_state_kind import FleetProfileSwitchChildStateKind
from ..models.fleet_profile_switch_child_state_state import check_fleet_profile_switch_child_state_state
from ..models.fleet_profile_switch_child_state_state import FleetProfileSwitchChildStateState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.fleet_profile_switch_child_result import FleetProfileSwitchChildResult





T = TypeVar("T", bound="FleetProfileSwitchChildState")



@_attrs_define
class FleetProfileSwitchChildState:
    """ Terminal receipt for a child already completed by the adapter.

        Attributes:
            kind (FleetProfileSwitchChildStateKind):
            operation_id (str):
            state (FleetProfileSwitchChildStateState):
            result (Union['FleetProfileSwitchChildResult', None, Unset]):
     """

    kind: FleetProfileSwitchChildStateKind
    operation_id: str
    state: FleetProfileSwitchChildStateState
    result: Union['FleetProfileSwitchChildResult', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_switch_child_result import FleetProfileSwitchChildResult
        kind: str = self.kind

        operation_id = self.operation_id

        state: str = self.state

        result: Union[None, Unset, dict[str, Any]]
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, FleetProfileSwitchChildResult):
            result = self.result.to_dict()
        else:
            result = self.result


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "kind": kind,
            "operation_id": operation_id,
            "state": state,
        })
        if result is not UNSET:
            field_dict["result"] = result

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_switch_child_result import FleetProfileSwitchChildResult
        d = dict(src_dict)
        kind = check_fleet_profile_switch_child_state_kind(d.pop("kind"))




        operation_id = d.pop("operation_id")

        state = check_fleet_profile_switch_child_state_state(d.pop("state"))




        def _parse_result(data: object) -> Union['FleetProfileSwitchChildResult', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = FleetProfileSwitchChildResult.from_dict(data)



                return result_type_0
            except: # noqa: E722
                pass
            return cast(Union['FleetProfileSwitchChildResult', None, Unset], data)

        result = _parse_result(d.pop("result", UNSET))


        fleet_profile_switch_child_state = cls(
            kind=kind,
            operation_id=operation_id,
            state=state,
            result=result,
        )

        return fleet_profile_switch_child_state
