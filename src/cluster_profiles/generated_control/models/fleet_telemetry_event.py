from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.telemetry_point import TelemetryPoint





T = TypeVar("T", bound="FleetTelemetryEvent")



@_attrs_define
class FleetTelemetryEvent:
    """
        Attributes:
            node_id (str):
            sample (TelemetryPoint):
     """

    node_id: str
    sample: TelemetryPoint





    def to_dict(self) -> dict[str, Any]:
        from ..models.telemetry_point import TelemetryPoint # noqa: PLC0415
        node_id = self.node_id

        sample = self.sample.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "sample": sample,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.telemetry_point import TelemetryPoint # noqa: PLC0415
        d = dict(src_dict)
        node_id = d.pop("node_id")

        sample = TelemetryPoint.from_dict(d.pop("sample"))




        fleet_telemetry_event = cls(
            node_id=node_id,
            sample=sample,
        )

        return fleet_telemetry_event
