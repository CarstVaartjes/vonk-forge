from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.telemetry_rollup_point_resolution import check_telemetry_rollup_point_resolution
from ..models.telemetry_rollup_point_resolution import TelemetryRollupPointResolution
from typing import cast
import datetime

if TYPE_CHECKING:
  from ..models.telemetry_rollup_point_metrics import TelemetryRollupPointMetrics





T = TypeVar("T", bound="TelemetryRollupPoint")



@_attrs_define
class TelemetryRollupPoint:
    """
        Attributes:
            bucket_end (datetime.datetime):
            bucket_start (datetime.datetime):
            gap_samples (int):
            metrics (TelemetryRollupPointMetrics):
            node_id (str):
            resolution (TelemetryRollupPointResolution):
            source_sample_count (int):
     """

    bucket_end: datetime.datetime
    bucket_start: datetime.datetime
    gap_samples: int
    metrics: TelemetryRollupPointMetrics
    node_id: str
    resolution: TelemetryRollupPointResolution
    source_sample_count: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.telemetry_rollup_point_metrics import TelemetryRollupPointMetrics # noqa: PLC0415
        bucket_end = self.bucket_end.isoformat()

        bucket_start = self.bucket_start.isoformat()

        gap_samples = self.gap_samples

        metrics = self.metrics.to_dict()

        node_id = self.node_id

        resolution: str = self.resolution

        source_sample_count = self.source_sample_count


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "bucket_end": bucket_end,
            "bucket_start": bucket_start,
            "gap_samples": gap_samples,
            "metrics": metrics,
            "node_id": node_id,
            "resolution": resolution,
            "source_sample_count": source_sample_count,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.telemetry_rollup_point_metrics import TelemetryRollupPointMetrics # noqa: PLC0415
        d = dict(src_dict)
        bucket_end = datetime.datetime.fromisoformat(d.pop("bucket_end"))




        bucket_start = datetime.datetime.fromisoformat(d.pop("bucket_start"))




        gap_samples = d.pop("gap_samples")

        metrics = TelemetryRollupPointMetrics.from_dict(d.pop("metrics"))




        node_id = d.pop("node_id")

        resolution = check_telemetry_rollup_point_resolution(d.pop("resolution"))




        source_sample_count = d.pop("source_sample_count")

        telemetry_rollup_point = cls(
            bucket_end=bucket_end,
            bucket_start=bucket_start,
            gap_samples=gap_samples,
            metrics=metrics,
            node_id=node_id,
            resolution=resolution,
            source_sample_count=source_sample_count,
        )

        return telemetry_rollup_point
