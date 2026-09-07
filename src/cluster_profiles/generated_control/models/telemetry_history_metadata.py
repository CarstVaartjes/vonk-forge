from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.telemetry_history_metadata_actual_resolution import check_telemetry_history_metadata_actual_resolution
from ..models.telemetry_history_metadata_actual_resolution import TelemetryHistoryMetadataActualResolution
from ..models.telemetry_history_metadata_requested_resolution import check_telemetry_history_metadata_requested_resolution
from ..models.telemetry_history_metadata_requested_resolution import TelemetryHistoryMetadataRequestedResolution
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union
import datetime






T = TypeVar("T", bound="TelemetryHistoryMetadata")



@_attrs_define
class TelemetryHistoryMetadata:
    """ Coverage and downsampling facts for a history/export response.

        Attributes:
            actual_resolution (TelemetryHistoryMetadataActualResolution):
            coverage_seconds (float):
            downsampled (bool):
            gap_samples (int):
            point_count (int):
            requested_end (datetime.datetime):
            requested_resolution (TelemetryHistoryMetadataRequestedResolution):
            requested_start (datetime.datetime):
            actual_end (Union[None, Unset, datetime.datetime]):
            actual_start (Union[None, Unset, datetime.datetime]):
            timezone (Union[Literal['UTC'], Unset]):  Default: 'UTC'.
     """

    actual_resolution: TelemetryHistoryMetadataActualResolution
    coverage_seconds: float
    downsampled: bool
    gap_samples: int
    point_count: int
    requested_end: datetime.datetime
    requested_resolution: TelemetryHistoryMetadataRequestedResolution
    requested_start: datetime.datetime
    actual_end: Union[None, Unset, datetime.datetime] = UNSET
    actual_start: Union[None, Unset, datetime.datetime] = UNSET
    timezone: Union[Literal['UTC'], Unset] = 'UTC'





    def to_dict(self) -> dict[str, Any]:
        actual_resolution: str = self.actual_resolution

        coverage_seconds = self.coverage_seconds

        downsampled = self.downsampled

        gap_samples = self.gap_samples

        point_count = self.point_count

        requested_end = self.requested_end.isoformat()

        requested_resolution: str = self.requested_resolution

        requested_start = self.requested_start.isoformat()

        actual_end: Union[None, Unset, str]
        if isinstance(self.actual_end, Unset):
            actual_end = UNSET
        elif isinstance(self.actual_end, datetime.datetime):
            actual_end = self.actual_end.isoformat()
        else:
            actual_end = self.actual_end

        actual_start: Union[None, Unset, str]
        if isinstance(self.actual_start, Unset):
            actual_start = UNSET
        elif isinstance(self.actual_start, datetime.datetime):
            actual_start = self.actual_start.isoformat()
        else:
            actual_start = self.actual_start

        timezone = self.timezone


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "actual_resolution": actual_resolution,
            "coverage_seconds": coverage_seconds,
            "downsampled": downsampled,
            "gap_samples": gap_samples,
            "point_count": point_count,
            "requested_end": requested_end,
            "requested_resolution": requested_resolution,
            "requested_start": requested_start,
        })
        if actual_end is not UNSET:
            field_dict["actual_end"] = actual_end
        if actual_start is not UNSET:
            field_dict["actual_start"] = actual_start
        if timezone is not UNSET:
            field_dict["timezone"] = timezone

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        actual_resolution = check_telemetry_history_metadata_actual_resolution(d.pop("actual_resolution"))




        coverage_seconds = d.pop("coverage_seconds")

        downsampled = d.pop("downsampled")

        gap_samples = d.pop("gap_samples")

        point_count = d.pop("point_count")

        requested_end = isoparse(d.pop("requested_end"))




        requested_resolution = check_telemetry_history_metadata_requested_resolution(d.pop("requested_resolution"))




        requested_start = isoparse(d.pop("requested_start"))




        def _parse_actual_end(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                actual_end_type_0 = isoparse(data)



                return actual_end_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        actual_end = _parse_actual_end(d.pop("actual_end", UNSET))


        def _parse_actual_start(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                actual_start_type_0 = isoparse(data)



                return actual_start_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        actual_start = _parse_actual_start(d.pop("actual_start", UNSET))


        timezone = cast(Union[Literal['UTC'], Unset] , d.pop("timezone", UNSET))
        if timezone != 'UTC' and not isinstance(timezone, Unset):
            raise ValueError(f"timezone must match const 'UTC', got '{timezone}'")

        telemetry_history_metadata = cls(
            actual_resolution=actual_resolution,
            coverage_seconds=coverage_seconds,
            downsampled=downsampled,
            gap_samples=gap_samples,
            point_count=point_count,
            requested_end=requested_end,
            requested_resolution=requested_resolution,
            requested_start=requested_start,
            actual_end=actual_end,
            actual_start=actual_start,
            timezone=timezone,
        )

        return telemetry_history_metadata
