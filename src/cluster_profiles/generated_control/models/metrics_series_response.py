from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.metrics_series_response_metric import check_metrics_series_response_metric
from ..models.metrics_series_response_metric import MetricsSeriesResponseMetric
from ..models.metrics_series_response_range import check_metrics_series_response_range
from ..models.metrics_series_response_range import MetricsSeriesResponseRange
from typing import cast

if TYPE_CHECKING:
  from ..models.metric_series import MetricSeries





T = TypeVar("T", bound="MetricsSeriesResponse")



@_attrs_define
class MetricsSeriesResponse:
    """
        Attributes:
            end (float):
            metric (MetricsSeriesResponseMetric):
            range_ (MetricsSeriesResponseRange):
            series (list[MetricSeries]):
            start (float):
            step_seconds (int):
     """

    end: float
    metric: MetricsSeriesResponseMetric
    range_: MetricsSeriesResponseRange
    series: list[MetricSeries]
    start: float
    step_seconds: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.metric_series import MetricSeries # noqa: PLC0415
        end = self.end

        metric: str = self.metric

        range_: str = self.range_

        series = []
        for series_item_data in self.series:
            series_item = series_item_data.to_dict()
            series.append(series_item)



        start = self.start

        step_seconds = self.step_seconds


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "end": end,
            "metric": metric,
            "range": range_,
            "series": series,
            "start": start,
            "step_seconds": step_seconds,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.metric_series import MetricSeries # noqa: PLC0415
        d = dict(src_dict)
        end = d.pop("end")

        metric = check_metrics_series_response_metric(d.pop("metric"))




        range_ = check_metrics_series_response_range(d.pop("range"))




        series = []
        _series = d.pop("series")
        for series_item_data in (_series):
            series_item = MetricSeries.from_dict(series_item_data)



            series.append(series_item)


        start = d.pop("start")

        step_seconds = d.pop("step_seconds")

        metrics_series_response = cls(
            end=end,
            metric=metric,
            range_=range_,
            series=series,
            start=start,
            step_seconds=step_seconds,
        )


        metrics_series_response.additional_properties = d
        return metrics_series_response

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
