from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.telemetry_point import TelemetryPoint





T = TypeVar("T", bound="FleetTelemetryEvent")



@_attrs_define
class FleetTelemetryEvent:
    """
        Attributes:
            node_id (str):
            sample (TelemetryPoint):
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    node_id: str
    sample: 'TelemetryPoint'
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        from ..models.telemetry_point import TelemetryPoint
        node_id = self.node_id

        sample = self.sample.to_dict()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "sample": sample,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.telemetry_point import TelemetryPoint
        d = dict(src_dict)
        node_id = d.pop("node_id")

        sample = TelemetryPoint.from_dict(d.pop("sample"))




        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        fleet_telemetry_event = cls(
            node_id=node_id,
            sample=sample,
            schema_version=schema_version,
        )

        return fleet_telemetry_event
