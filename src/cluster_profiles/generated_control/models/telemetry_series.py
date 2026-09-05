from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.telemetry_series_freshness import check_telemetry_series_freshness
from ..models.telemetry_series_freshness import TelemetrySeriesFreshness
from ..models.telemetry_series_measurement_kind import check_telemetry_series_measurement_kind
from ..models.telemetry_series_measurement_kind import TelemetrySeriesMeasurementKind
from ..models.telemetry_series_scope import check_telemetry_series_scope
from ..models.telemetry_series_scope import TelemetrySeriesScope
from ..models.telemetry_series_support_status import check_telemetry_series_support_status
from ..models.telemetry_series_support_status import TelemetrySeriesSupportStatus
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime






T = TypeVar("T", bound="TelemetrySeries")



@_attrs_define
class TelemetrySeries:
    """ One sampled or configured metric in canonical units.

        Attributes:
            aggregation (str):
            freshness_threshold_seconds (float):
            key (str):
            measurement_kind (TelemetrySeriesMeasurementKind):
            observed_at (datetime.datetime):
            scope (TelemetrySeriesScope):
            source (str):
            support_status (TelemetrySeriesSupportStatus):
            unit (str):
            value (Union[None, bool, float, int, str]):
            device_id (Union[None, Unset, str]):
            freshness (Union[Unset, TelemetrySeriesFreshness]):  Default: 'fresh'.
            interface_name (Union[None, Unset, str]):
            node_id (Union[None, Unset, str]):
            process_id (Union[None, Unset, int]):
            process_name (Union[None, Unset, str]):
            reason (Union[None, Unset, str]):
            received_at (Union[None, Unset, datetime.datetime]):
            run_id (Union[None, Unset, str]):
     """

    aggregation: str
    freshness_threshold_seconds: float
    key: str
    measurement_kind: TelemetrySeriesMeasurementKind
    observed_at: datetime.datetime
    scope: TelemetrySeriesScope
    source: str
    support_status: TelemetrySeriesSupportStatus
    unit: str
    value: Union[None, bool, float, int, str]
    device_id: Union[None, Unset, str] = UNSET
    freshness: Union[Unset, TelemetrySeriesFreshness] = 'fresh'
    interface_name: Union[None, Unset, str] = UNSET
    node_id: Union[None, Unset, str] = UNSET
    process_id: Union[None, Unset, int] = UNSET
    process_name: Union[None, Unset, str] = UNSET
    reason: Union[None, Unset, str] = UNSET
    received_at: Union[None, Unset, datetime.datetime] = UNSET
    run_id: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        aggregation = self.aggregation

        freshness_threshold_seconds = self.freshness_threshold_seconds

        key = self.key

        measurement_kind: str = self.measurement_kind

        observed_at = self.observed_at.isoformat()

        scope: str = self.scope

        source = self.source

        support_status: str = self.support_status

        unit = self.unit

        value: Union[None, bool, float, int, str]
        value = self.value

        device_id: Union[None, Unset, str]
        if isinstance(self.device_id, Unset):
            device_id = UNSET
        else:
            device_id = self.device_id

        freshness: Union[Unset, str] = UNSET
        if not isinstance(self.freshness, Unset):
            freshness = self.freshness


        interface_name: Union[None, Unset, str]
        if isinstance(self.interface_name, Unset):
            interface_name = UNSET
        else:
            interface_name = self.interface_name

        node_id: Union[None, Unset, str]
        if isinstance(self.node_id, Unset):
            node_id = UNSET
        else:
            node_id = self.node_id

        process_id: Union[None, Unset, int]
        if isinstance(self.process_id, Unset):
            process_id = UNSET
        else:
            process_id = self.process_id

        process_name: Union[None, Unset, str]
        if isinstance(self.process_name, Unset):
            process_name = UNSET
        else:
            process_name = self.process_name

        reason: Union[None, Unset, str]
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        received_at: Union[None, Unset, str]
        if isinstance(self.received_at, Unset):
            received_at = UNSET
        elif isinstance(self.received_at, datetime.datetime):
            received_at = self.received_at.isoformat()
        else:
            received_at = self.received_at

        run_id: Union[None, Unset, str]
        if isinstance(self.run_id, Unset):
            run_id = UNSET
        else:
            run_id = self.run_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "aggregation": aggregation,
            "freshness_threshold_seconds": freshness_threshold_seconds,
            "key": key,
            "measurement_kind": measurement_kind,
            "observed_at": observed_at,
            "scope": scope,
            "source": source,
            "support_status": support_status,
            "unit": unit,
            "value": value,
        })
        if device_id is not UNSET:
            field_dict["device_id"] = device_id
        if freshness is not UNSET:
            field_dict["freshness"] = freshness
        if interface_name is not UNSET:
            field_dict["interface_name"] = interface_name
        if node_id is not UNSET:
            field_dict["node_id"] = node_id
        if process_id is not UNSET:
            field_dict["process_id"] = process_id
        if process_name is not UNSET:
            field_dict["process_name"] = process_name
        if reason is not UNSET:
            field_dict["reason"] = reason
        if received_at is not UNSET:
            field_dict["received_at"] = received_at
        if run_id is not UNSET:
            field_dict["run_id"] = run_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        aggregation = d.pop("aggregation")

        freshness_threshold_seconds = d.pop("freshness_threshold_seconds")

        key = d.pop("key")

        measurement_kind = check_telemetry_series_measurement_kind(d.pop("measurement_kind"))




        observed_at = isoparse(d.pop("observed_at"))




        scope = check_telemetry_series_scope(d.pop("scope"))




        source = d.pop("source")

        support_status = check_telemetry_series_support_status(d.pop("support_status"))




        unit = d.pop("unit")

        def _parse_value(data: object) -> Union[None, bool, float, int, str]:
            if data is None:
                return data
            return cast(Union[None, bool, float, int, str], data)

        value = _parse_value(d.pop("value"))


        def _parse_device_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        device_id = _parse_device_id(d.pop("device_id", UNSET))


        _freshness = d.pop("freshness", UNSET)
        freshness: Union[Unset, TelemetrySeriesFreshness]
        if isinstance(_freshness,  Unset):
            freshness = UNSET
        else:
            freshness = check_telemetry_series_freshness(_freshness)




        def _parse_interface_name(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        interface_name = _parse_interface_name(d.pop("interface_name", UNSET))


        def _parse_node_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        node_id = _parse_node_id(d.pop("node_id", UNSET))


        def _parse_process_id(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        process_id = _parse_process_id(d.pop("process_id", UNSET))


        def _parse_process_name(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        process_name = _parse_process_name(d.pop("process_name", UNSET))


        def _parse_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        reason = _parse_reason(d.pop("reason", UNSET))


        def _parse_received_at(data: object) -> Union[None, Unset, datetime.datetime]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                received_at_type_0 = isoparse(data)



                return received_at_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, datetime.datetime], data)

        received_at = _parse_received_at(d.pop("received_at", UNSET))


        def _parse_run_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        run_id = _parse_run_id(d.pop("run_id", UNSET))


        telemetry_series = cls(
            aggregation=aggregation,
            freshness_threshold_seconds=freshness_threshold_seconds,
            key=key,
            measurement_kind=measurement_kind,
            observed_at=observed_at,
            scope=scope,
            source=source,
            support_status=support_status,
            unit=unit,
            value=value,
            device_id=device_id,
            freshness=freshness,
            interface_name=interface_name,
            node_id=node_id,
            process_id=process_id,
            process_name=process_name,
            reason=reason,
            received_at=received_at,
            run_id=run_id,
        )

        return telemetry_series
