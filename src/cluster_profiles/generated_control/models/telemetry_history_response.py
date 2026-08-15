from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.telemetry_history_response_resolution import check_telemetry_history_response_resolution
from ..models.telemetry_history_response_resolution import TelemetryHistoryResponseResolution
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
import datetime

if TYPE_CHECKING:
  from ..models.telemetry_point import TelemetryPoint
  from ..models.telemetry_rollup_point import TelemetryRollupPoint





T = TypeVar("T", bound="TelemetryHistoryResponse")



@_attrs_define
class TelemetryHistoryResponse:
    """
        Attributes:
            end (datetime.datetime):
            maximum_points (int):
            node_id (str):
            points (list[Union['TelemetryPoint', 'TelemetryRollupPoint']]):
            resolution (TelemetryHistoryResponseResolution):
            start (datetime.datetime):
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    end: datetime.datetime
    maximum_points: int
    node_id: str
    points: list[Union['TelemetryPoint', 'TelemetryRollupPoint']]
    resolution: TelemetryHistoryResponseResolution
    start: datetime.datetime
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        from ..models.telemetry_point import TelemetryPoint
        from ..models.telemetry_rollup_point import TelemetryRollupPoint
        end = self.end.isoformat()

        maximum_points = self.maximum_points

        node_id = self.node_id

        points = []
        for points_item_data in self.points:
            points_item: dict[str, Any]
            if isinstance(points_item_data, TelemetryPoint):
                points_item = points_item_data.to_dict()
            else:
                points_item = points_item_data.to_dict()

            points.append(points_item)



        resolution: str = self.resolution

        start = self.start.isoformat()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "end": end,
            "maximum_points": maximum_points,
            "node_id": node_id,
            "points": points,
            "resolution": resolution,
            "start": start,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.telemetry_point import TelemetryPoint
        from ..models.telemetry_rollup_point import TelemetryRollupPoint
        d = dict(src_dict)
        end = isoparse(d.pop("end"))




        maximum_points = d.pop("maximum_points")

        node_id = d.pop("node_id")

        points = []
        _points = d.pop("points")
        for points_item_data in (_points):
            def _parse_points_item(data: object) -> Union['TelemetryPoint', 'TelemetryRollupPoint']:
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    points_item_type_0 = TelemetryPoint.from_dict(data)



                    return points_item_type_0
                except: # noqa: E722
                    pass
                if not isinstance(data, dict):
                    raise TypeError()
                points_item_type_1 = TelemetryRollupPoint.from_dict(data)



                return points_item_type_1

            points_item = _parse_points_item(points_item_data)

            points.append(points_item)


        resolution = check_telemetry_history_response_resolution(d.pop("resolution"))




        start = isoparse(d.pop("start"))




        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        telemetry_history_response = cls(
            end=end,
            maximum_points=maximum_points,
            node_id=node_id,
            points=points,
            resolution=resolution,
            start=start,
            schema_version=schema_version,
        )

        return telemetry_history_response
