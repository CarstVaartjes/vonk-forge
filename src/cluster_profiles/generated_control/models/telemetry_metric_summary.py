from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="TelemetryMetricSummary")



@_attrs_define
class TelemetryMetricSummary:
    """
        Attributes:
            count (int):
            maximum (float):
            mean (float):
            minimum (float):
            aggregation (Union[Unset, str]):  Default: 'mean'.
            device_id (Union[None, Unset, str]):
            interface_name (Union[None, Unset, str]):
            key (Union[None, Unset, str]):
            measurement_kind (Union[Unset, str]):  Default: 'measured'.
            process_id (Union[None, Unset, int]):
            process_name (Union[None, Unset, str]):
            run_id (Union[None, Unset, str]):
            scope (Union[None, Unset, str]):
            source (Union[Unset, str]):  Default: 'legacy'.
            unit (Union[Unset, str]):  Default: 'unknown'.
     """

    count: int
    maximum: float
    mean: float
    minimum: float
    aggregation: Union[Unset, str] = 'mean'
    device_id: Union[None, Unset, str] = UNSET
    interface_name: Union[None, Unset, str] = UNSET
    key: Union[None, Unset, str] = UNSET
    measurement_kind: Union[Unset, str] = 'measured'
    process_id: Union[None, Unset, int] = UNSET
    process_name: Union[None, Unset, str] = UNSET
    run_id: Union[None, Unset, str] = UNSET
    scope: Union[None, Unset, str] = UNSET
    source: Union[Unset, str] = 'legacy'
    unit: Union[Unset, str] = 'unknown'





    def to_dict(self) -> dict[str, Any]:
        count = self.count

        maximum = self.maximum

        mean = self.mean

        minimum = self.minimum

        aggregation = self.aggregation

        device_id: Union[None, Unset, str]
        if isinstance(self.device_id, Unset):
            device_id = UNSET
        else:
            device_id = self.device_id

        interface_name: Union[None, Unset, str]
        if isinstance(self.interface_name, Unset):
            interface_name = UNSET
        else:
            interface_name = self.interface_name

        key: Union[None, Unset, str]
        if isinstance(self.key, Unset):
            key = UNSET
        else:
            key = self.key

        measurement_kind = self.measurement_kind

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

        run_id: Union[None, Unset, str]
        if isinstance(self.run_id, Unset):
            run_id = UNSET
        else:
            run_id = self.run_id

        scope: Union[None, Unset, str]
        if isinstance(self.scope, Unset):
            scope = UNSET
        else:
            scope = self.scope

        source = self.source

        unit = self.unit


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "count": count,
            "maximum": maximum,
            "mean": mean,
            "minimum": minimum,
        })
        if aggregation is not UNSET:
            field_dict["aggregation"] = aggregation
        if device_id is not UNSET:
            field_dict["device_id"] = device_id
        if interface_name is not UNSET:
            field_dict["interface_name"] = interface_name
        if key is not UNSET:
            field_dict["key"] = key
        if measurement_kind is not UNSET:
            field_dict["measurement_kind"] = measurement_kind
        if process_id is not UNSET:
            field_dict["process_id"] = process_id
        if process_name is not UNSET:
            field_dict["process_name"] = process_name
        if run_id is not UNSET:
            field_dict["run_id"] = run_id
        if scope is not UNSET:
            field_dict["scope"] = scope
        if source is not UNSET:
            field_dict["source"] = source
        if unit is not UNSET:
            field_dict["unit"] = unit

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        count = d.pop("count")

        maximum = d.pop("maximum")

        mean = d.pop("mean")

        minimum = d.pop("minimum")

        aggregation = d.pop("aggregation", UNSET)

        def _parse_device_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        device_id = _parse_device_id(d.pop("device_id", UNSET))


        def _parse_interface_name(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        interface_name = _parse_interface_name(d.pop("interface_name", UNSET))


        def _parse_key(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        key = _parse_key(d.pop("key", UNSET))


        measurement_kind = d.pop("measurement_kind", UNSET)

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


        def _parse_run_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        run_id = _parse_run_id(d.pop("run_id", UNSET))


        def _parse_scope(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        scope = _parse_scope(d.pop("scope", UNSET))


        source = d.pop("source", UNSET)

        unit = d.pop("unit", UNSET)

        telemetry_metric_summary = cls(
            count=count,
            maximum=maximum,
            mean=mean,
            minimum=minimum,
            aggregation=aggregation,
            device_id=device_id,
            interface_name=interface_name,
            key=key,
            measurement_kind=measurement_kind,
            process_id=process_id,
            process_name=process_name,
            run_id=run_id,
            scope=scope,
            source=source,
            unit=unit,
        )

        return telemetry_metric_summary
