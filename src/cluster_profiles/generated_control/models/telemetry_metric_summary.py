from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="TelemetryMetricSummary")



@_attrs_define
class TelemetryMetricSummary:
    """
        Attributes:
            count (int):
            maximum (float):
            mean (float):
            minimum (float):
            aggregation (str | Unset):  Default: 'mean'.
            device_id (None | str | Unset):
            interface_name (None | str | Unset):
            key (None | str | Unset):
            measurement_kind (str | Unset):  Default: 'measured'.
            process_id (int | None | Unset):
            process_name (None | str | Unset):
            run_id (None | str | Unset):
            scope (None | str | Unset):
            source (str | Unset):  Default: 'controller-derived'.
            unit (str | Unset):  Default: 'unknown'.
     """

    count: int
    maximum: float
    mean: float
    minimum: float
    aggregation: str | Unset = 'mean'
    device_id: None | str | Unset = UNSET
    interface_name: None | str | Unset = UNSET
    key: None | str | Unset = UNSET
    measurement_kind: str | Unset = 'measured'
    process_id: int | None | Unset = UNSET
    process_name: None | str | Unset = UNSET
    run_id: None | str | Unset = UNSET
    scope: None | str | Unset = UNSET
    source: str | Unset = 'controller-derived'
    unit: str | Unset = 'unknown'





    def to_dict(self) -> dict[str, Any]:
        count = self.count

        maximum = self.maximum

        mean = self.mean

        minimum = self.minimum

        aggregation = self.aggregation

        device_id: None | str | Unset
        if isinstance(self.device_id, Unset):
            device_id = UNSET
        else:
            device_id = self.device_id

        interface_name: None | str | Unset
        if isinstance(self.interface_name, Unset):
            interface_name = UNSET
        else:
            interface_name = self.interface_name

        key: None | str | Unset
        if isinstance(self.key, Unset):
            key = UNSET
        else:
            key = self.key

        measurement_kind = self.measurement_kind

        process_id: int | None | Unset
        if isinstance(self.process_id, Unset):
            process_id = UNSET
        else:
            process_id = self.process_id

        process_name: None | str | Unset
        if isinstance(self.process_name, Unset):
            process_name = UNSET
        else:
            process_name = self.process_name

        run_id: None | str | Unset
        if isinstance(self.run_id, Unset):
            run_id = UNSET
        else:
            run_id = self.run_id

        scope: None | str | Unset
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

        def _parse_device_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        device_id = _parse_device_id(d.pop("device_id", UNSET))


        def _parse_interface_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        interface_name = _parse_interface_name(d.pop("interface_name", UNSET))


        def _parse_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        key = _parse_key(d.pop("key", UNSET))


        measurement_kind = d.pop("measurement_kind", UNSET)

        def _parse_process_id(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        process_id = _parse_process_id(d.pop("process_id", UNSET))


        def _parse_process_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        process_name = _parse_process_name(d.pop("process_name", UNSET))


        def _parse_run_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        run_id = _parse_run_id(d.pop("run_id", UNSET))


        def _parse_scope(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

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
