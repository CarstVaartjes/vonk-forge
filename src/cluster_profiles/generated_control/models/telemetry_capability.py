from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.telemetry_capability_measurement_kind import check_telemetry_capability_measurement_kind
from ..models.telemetry_capability_measurement_kind import TelemetryCapabilityMeasurementKind
from ..models.telemetry_capability_scope import check_telemetry_capability_scope
from ..models.telemetry_capability_scope import TelemetryCapabilityScope
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="TelemetryCapability")



@_attrs_define
class TelemetryCapability:
    """ Capability inventory, including explicitly unsupported sensors.

        Attributes:
            freshness_threshold_seconds (float):
            key (str):
            measurement_kind (TelemetryCapabilityMeasurementKind):
            scope (TelemetryCapabilityScope):
            source (str):
            supported (bool):
            unit (str):
            device_id (Union[None, Unset, str]):
            interface_name (Union[None, Unset, str]):
            node_id (Union[None, Unset, str]):
            process_id (Union[None, Unset, int]):
            process_name (Union[None, Unset, str]):
            reason (Union[None, Unset, str]):
            run_id (Union[None, Unset, str]):
     """

    freshness_threshold_seconds: float
    key: str
    measurement_kind: TelemetryCapabilityMeasurementKind
    scope: TelemetryCapabilityScope
    source: str
    supported: bool
    unit: str
    device_id: Union[None, Unset, str] = UNSET
    interface_name: Union[None, Unset, str] = UNSET
    node_id: Union[None, Unset, str] = UNSET
    process_id: Union[None, Unset, int] = UNSET
    process_name: Union[None, Unset, str] = UNSET
    reason: Union[None, Unset, str] = UNSET
    run_id: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        freshness_threshold_seconds = self.freshness_threshold_seconds

        key = self.key

        measurement_kind: str = self.measurement_kind

        scope: str = self.scope

        source = self.source

        supported = self.supported

        unit = self.unit

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

        run_id: Union[None, Unset, str]
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


        def _parse_run_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

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
