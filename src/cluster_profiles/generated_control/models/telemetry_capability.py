from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.telemetry_capability_measurement_kind import check_telemetry_capability_measurement_kind
from ..models.telemetry_capability_measurement_kind import TelemetryCapabilityMeasurementKind
from ..models.telemetry_capability_scope import check_telemetry_capability_scope
from ..models.telemetry_capability_scope import TelemetryCapabilityScope
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="TelemetryCapability")



@_attrs_define
class TelemetryCapability:
    """
        Attributes:
            freshness_threshold_seconds (float):
            key (str):
            measurement_kind (TelemetryCapabilityMeasurementKind):
            scope (TelemetryCapabilityScope):
            source (str):
            supported (bool):
            unit (str):
            device_id (None | str | Unset):
            interface_name (None | str | Unset):
            node_id (None | str | Unset):
            process_id (int | None | Unset):
            process_name (None | str | Unset):
            reason (None | str | Unset):
            run_id (None | str | Unset):
     """

    freshness_threshold_seconds: float
    key: str
    measurement_kind: TelemetryCapabilityMeasurementKind
    scope: TelemetryCapabilityScope
    source: str
    supported: bool
    unit: str
    device_id: None | str | Unset = UNSET
    interface_name: None | str | Unset = UNSET
    node_id: None | str | Unset = UNSET
    process_id: int | None | Unset = UNSET
    process_name: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    run_id: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        freshness_threshold_seconds = self.freshness_threshold_seconds

        key = self.key

        measurement_kind: str = self.measurement_kind

        scope: str = self.scope

        source = self.source

        supported = self.supported

        unit = self.unit

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

        node_id: None | str | Unset
        if isinstance(self.node_id, Unset):
            node_id = UNSET
        else:
            node_id = self.node_id

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

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        run_id: None | str | Unset
        if isinstance(self.run_id, Unset):
            run_id = UNSET
        else:
            run_id = self.run_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "freshness_threshold_seconds": freshness_threshold_seconds,
            "key": key,
            "measurement_kind": measurement_kind,
            "scope": scope,
            "source": source,
            "supported": supported,
            "unit": unit,
        })
        if device_id is not UNSET:
            field_dict["device_id"] = device_id
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
        if run_id is not UNSET:
            field_dict["run_id"] = run_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        freshness_threshold_seconds = d.pop("freshness_threshold_seconds")

        key = d.pop("key")

        measurement_kind = check_telemetry_capability_measurement_kind(d.pop("measurement_kind"))




        scope = check_telemetry_capability_scope(d.pop("scope"))




        source = d.pop("source")

        supported = d.pop("supported")

        unit = d.pop("unit")

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


        def _parse_node_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        node_id = _parse_node_id(d.pop("node_id", UNSET))


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


        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))


        def _parse_run_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        run_id = _parse_run_id(d.pop("run_id", UNSET))


        telemetry_capability = cls(
            freshness_threshold_seconds=freshness_threshold_seconds,
            key=key,
            measurement_kind=measurement_kind,
            scope=scope,
            source=source,
            supported=supported,
            unit=unit,
            device_id=device_id,
            interface_name=interface_name,
            node_id=node_id,
            process_id=process_id,
            process_name=process_name,
            reason=reason,
            run_id=run_id,
        )

        return telemetry_capability
