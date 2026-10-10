from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.metric_point import MetricPoint
  from ..models.metric_series_labels import MetricSeriesLabels





T = TypeVar("T", bound="MetricSeries")



@_attrs_define
class MetricSeries:
    """
        Attributes:
            labels (MetricSeriesLabels):
            points (list[MetricPoint]):
     """

    labels: MetricSeriesLabels
    points: list[MetricPoint]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.metric_point import MetricPoint # noqa: PLC0415
        from ..models.metric_series_labels import MetricSeriesLabels # noqa: PLC0415
        labels = self.labels.to_dict()

        points = []
        for points_item_data in self.points:
            points_item = points_item_data.to_dict()
            points.append(points_item)




        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "labels": labels,
            "points": points,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.metric_point import MetricPoint # noqa: PLC0415
        from ..models.metric_series_labels import MetricSeriesLabels # noqa: PLC0415
        d = dict(src_dict)
        labels = MetricSeriesLabels.from_dict(d.pop("labels"))




        points = []
        _points = d.pop("points")
        for points_item_data in (_points):
            points_item = MetricPoint.from_dict(points_item_data)



            points.append(points_item)


        metric_series = cls(
            labels=labels,
            points=points,
        )


        metric_series.additional_properties = d
        return metric_series

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
