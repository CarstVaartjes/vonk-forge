from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_switch_queue_item_kind import check_fleet_profile_switch_queue_item_kind
from ..models.fleet_profile_switch_queue_item_kind import FleetProfileSwitchQueueItemKind
from typing import cast






T = TypeVar("T", bound="FleetProfileSwitchQueueItem")



@_attrs_define
class FleetProfileSwitchQueueItem:
    """ One durable Run/Switch child in the profile reconciliation queue.

        Attributes:
            id (str):
            kind (FleetProfileSwitchQueueItemKind):
     """

    id: str
    kind: FleetProfileSwitchQueueItemKind





    def to_dict(self) -> dict[str, Any]:
        id = self.id

        kind: str = self.kind


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "kind": kind,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        kind = check_fleet_profile_switch_queue_item_kind(d.pop("kind"))




        fleet_profile_switch_queue_item = cls(
            id=id,
            kind=kind,
        )

        return fleet_profile_switch_queue_item
