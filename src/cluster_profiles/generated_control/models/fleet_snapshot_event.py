from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_snapshot import FleetSnapshot





T = TypeVar("T", bound="FleetSnapshotEvent")



@_attrs_define
class FleetSnapshotEvent:
    """
        Attributes:
            reset_reason (str):
            snapshot (FleetSnapshot):
     """

    reset_reason: str
    snapshot: FleetSnapshot





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_snapshot import FleetSnapshot # noqa: PLC0415
        reset_reason = self.reset_reason

        snapshot = self.snapshot.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "reset_reason": reset_reason,
            "snapshot": snapshot,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_snapshot import FleetSnapshot # noqa: PLC0415
        d = dict(src_dict)
        reset_reason = d.pop("reset_reason")

        snapshot = FleetSnapshot.from_dict(d.pop("snapshot"))




        fleet_snapshot_event = cls(
            reset_reason=reset_reason,
            snapshot=snapshot,
        )

        return fleet_snapshot_event
