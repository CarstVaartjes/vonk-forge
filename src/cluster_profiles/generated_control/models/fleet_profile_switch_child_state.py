from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_switch_child_state_kind import check_fleet_profile_switch_child_state_kind
from ..models.fleet_profile_switch_child_state_kind import FleetProfileSwitchChildStateKind
from ..models.fleet_profile_switch_child_state_state import check_fleet_profile_switch_child_state_state
from ..models.fleet_profile_switch_child_state_state import FleetProfileSwitchChildStateState
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_switch_child_result import FleetProfileSwitchChildResult





T = TypeVar("T", bound="FleetProfileSwitchChildState")



@_attrs_define
class FleetProfileSwitchChildState:
    """ Terminal receipt for a child already completed by the adapter.

        Attributes:
            kind (FleetProfileSwitchChildStateKind):
            operation_id (str):
            queue_index (int):
            state (FleetProfileSwitchChildStateState):
            original_operation_id (None | str | Unset):
            result (FleetProfileSwitchChildResult | None | Unset):
     """

    kind: FleetProfileSwitchChildStateKind
    operation_id: str
    queue_index: int
    state: FleetProfileSwitchChildStateState
    original_operation_id: None | str | Unset = UNSET
    result: FleetProfileSwitchChildResult | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_switch_child_result import FleetProfileSwitchChildResult # noqa: PLC0415
        kind: str = self.kind

        operation_id = self.operation_id

        queue_index = self.queue_index

        state: str = self.state

        original_operation_id: None | str | Unset
        if isinstance(self.original_operation_id, Unset):
            original_operation_id = UNSET
        else:
            original_operation_id = self.original_operation_id

        result: dict[str, Any] | None | Unset
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
            "queue_index": queue_index,
            "state": state,
        })
        if original_operation_id is not UNSET:
            field_dict["original_operation_id"] = original_operation_id
        if result is not UNSET:
            field_dict["result"] = result

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_switch_child_result import FleetProfileSwitchChildResult # noqa: PLC0415
        d = dict(src_dict)
        kind = check_fleet_profile_switch_child_state_kind(d.pop("kind"))




        operation_id = d.pop("operation_id")

        queue_index = d.pop("queue_index")

        state = check_fleet_profile_switch_child_state_state(d.pop("state"))




        def _parse_original_operation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        original_operation_id = _parse_original_operation_id(d.pop("original_operation_id", UNSET))


        def _parse_result(data: object) -> FleetProfileSwitchChildResult | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = FleetProfileSwitchChildResult.from_dict(data)



                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileSwitchChildResult | None | Unset, data)

        result = _parse_result(d.pop("result", UNSET))


        fleet_profile_switch_child_state = cls(
            kind=kind,
            operation_id=operation_id,
            queue_index=queue_index,
            state=state,
            original_operation_id=original_operation_id,
            result=result,
        )

        return fleet_profile_switch_child_state
