from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_switch_pending_child_kind import check_fleet_profile_switch_pending_child_kind
from ..models.fleet_profile_switch_pending_child_kind import FleetProfileSwitchPendingChildKind
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="FleetProfileSwitchPendingChild")



@_attrs_define
class FleetProfileSwitchPendingChild:
    """ An issued queue child whose exact outcome remains to be observed.

        Attributes:
            kind (FleetProfileSwitchPendingChildKind):
            operation_id (str):
            queue_index (int):
            original_operation_id (None | str | Unset):
     """

    kind: FleetProfileSwitchPendingChildKind
    operation_id: str
    queue_index: int
    original_operation_id: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        kind: str = self.kind

        operation_id = self.operation_id

        queue_index = self.queue_index

        original_operation_id: None | str | Unset
        if isinstance(self.original_operation_id, Unset):
            original_operation_id = UNSET
        else:
            original_operation_id = self.original_operation_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "kind": kind,
            "operation_id": operation_id,
            "queue_index": queue_index,
        })
        if original_operation_id is not UNSET:
            field_dict["original_operation_id"] = original_operation_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = check_fleet_profile_switch_pending_child_kind(d.pop("kind"))




        operation_id = d.pop("operation_id")

        queue_index = d.pop("queue_index")

        def _parse_original_operation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        original_operation_id = _parse_original_operation_id(d.pop("original_operation_id", UNSET))


        fleet_profile_switch_pending_child = cls(
            kind=kind,
            operation_id=operation_id,
            queue_index=queue_index,
            original_operation_id=original_operation_id,
        )

        return fleet_profile_switch_pending_child
