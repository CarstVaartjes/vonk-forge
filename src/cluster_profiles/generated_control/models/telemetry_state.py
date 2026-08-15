from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.telemetry_state_freshness import check_telemetry_state_freshness
from ..models.telemetry_state_freshness import TelemetryStateFreshness
from typing import cast

if TYPE_CHECKING:
  from ..models.telemetry_point import TelemetryPoint





T = TypeVar("T", bound="TelemetryState")



@_attrs_define
class TelemetryState:
    """
        Attributes:
            age_seconds (float):
            freshness (TelemetryStateFreshness):
            sample (TelemetryPoint):
     """

    age_seconds: float
    freshness: TelemetryStateFreshness
    sample: 'TelemetryPoint'





    def to_dict(self) -> dict[str, Any]:
        from ..models.telemetry_point import TelemetryPoint
        age_seconds = self.age_seconds

        freshness: str = self.freshness

        sample = self.sample.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "age_seconds": age_seconds,
            "freshness": freshness,
            "sample": sample,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.telemetry_point import TelemetryPoint
        d = dict(src_dict)
        age_seconds = d.pop("age_seconds")

        freshness = check_telemetry_state_freshness(d.pop("freshness"))




        sample = TelemetryPoint.from_dict(d.pop("sample"))




        telemetry_state = cls(
            age_seconds=age_seconds,
            freshness=freshness,
            sample=sample,
        )

        return telemetry_state
