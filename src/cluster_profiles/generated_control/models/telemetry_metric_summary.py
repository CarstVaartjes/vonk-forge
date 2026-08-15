from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="TelemetryMetricSummary")



@_attrs_define
class TelemetryMetricSummary:
    """
        Attributes:
            count (int):
            maximum (float):
            mean (float):
            minimum (float):
     """

    count: int
    maximum: float
    mean: float
    minimum: float





    def to_dict(self) -> dict[str, Any]:
        count = self.count

        maximum = self.maximum

        mean = self.mean

        minimum = self.minimum


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "count": count,
            "maximum": maximum,
            "mean": mean,
            "minimum": minimum,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        count = d.pop("count")

        maximum = d.pop("maximum")

        mean = d.pop("mean")

        minimum = d.pop("minimum")

        telemetry_metric_summary = cls(
            count=count,
            maximum=maximum,
            mean=mean,
            minimum=minimum,
        )

        return telemetry_metric_summary
