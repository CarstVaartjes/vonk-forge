from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.gpu_unavailable_reason import check_gpu_unavailable_reason
from ..models.gpu_unavailable_reason import GpuUnavailableReason
from ..types import UNSET, Unset
from typing import cast
import datetime






T = TypeVar("T", bound="TelemetryPoint")



@_attrs_define
class TelemetryPoint:
    """
        Attributes:
            boot_id (str):
            id (str):
            node_id (str):
            observed_at (datetime.datetime):
            received_at (datetime.datetime):
            cpu_frequency_avg_mhz (int | None | Unset):
            cpu_frequency_max_mhz (int | None | Unset):
            cpu_frequency_min_mhz (int | None | Unset):
            credential_remaining_fraction (float | None | Unset):
            disk_free_bytes (int | None | Unset):
            disk_total_bytes (int | None | Unset):
            gpu_memory_free_bytes (int | None | Unset):
            gpu_memory_total_bytes (int | None | Unset):
            gpu_temperature_c (int | None | Unset):
            gpu_unavailable_reason (GpuUnavailableReason | None | Unset):
            gpu_utilization_percent (float | None | Unset):
            memory_available_bytes (int | None | Unset):
            memory_total_bytes (int | None | Unset):
            renewal_failed (bool | None | Unset):
     """

    boot_id: str
    id: str
    node_id: str
    observed_at: datetime.datetime
    received_at: datetime.datetime
    cpu_frequency_avg_mhz: int | None | Unset = UNSET
    cpu_frequency_max_mhz: int | None | Unset = UNSET
    cpu_frequency_min_mhz: int | None | Unset = UNSET
    credential_remaining_fraction: float | None | Unset = UNSET
    disk_free_bytes: int | None | Unset = UNSET
    disk_total_bytes: int | None | Unset = UNSET
    gpu_memory_free_bytes: int | None | Unset = UNSET
    gpu_memory_total_bytes: int | None | Unset = UNSET
    gpu_temperature_c: int | None | Unset = UNSET
    gpu_unavailable_reason: GpuUnavailableReason | None | Unset = UNSET
    gpu_utilization_percent: float | None | Unset = UNSET
    memory_available_bytes: int | None | Unset = UNSET
    memory_total_bytes: int | None | Unset = UNSET
    renewal_failed: bool | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        boot_id = self.boot_id

        id = self.id

        node_id = self.node_id

        observed_at = self.observed_at.isoformat()

        received_at = self.received_at.isoformat()

        cpu_frequency_avg_mhz: int | None | Unset
        if isinstance(self.cpu_frequency_avg_mhz, Unset):
            cpu_frequency_avg_mhz = UNSET
        else:
            cpu_frequency_avg_mhz = self.cpu_frequency_avg_mhz

        cpu_frequency_max_mhz: int | None | Unset
        if isinstance(self.cpu_frequency_max_mhz, Unset):
            cpu_frequency_max_mhz = UNSET
        else:
            cpu_frequency_max_mhz = self.cpu_frequency_max_mhz

        cpu_frequency_min_mhz: int | None | Unset
        if isinstance(self.cpu_frequency_min_mhz, Unset):
            cpu_frequency_min_mhz = UNSET
        else:
            cpu_frequency_min_mhz = self.cpu_frequency_min_mhz

        credential_remaining_fraction: float | None | Unset
        if isinstance(self.credential_remaining_fraction, Unset):
            credential_remaining_fraction = UNSET
        else:
            credential_remaining_fraction = self.credential_remaining_fraction

        disk_free_bytes: int | None | Unset
        if isinstance(self.disk_free_bytes, Unset):
            disk_free_bytes = UNSET
        else:
            disk_free_bytes = self.disk_free_bytes

        disk_total_bytes: int | None | Unset
        if isinstance(self.disk_total_bytes, Unset):
            disk_total_bytes = UNSET
        else:
            disk_total_bytes = self.disk_total_bytes

        gpu_memory_free_bytes: int | None | Unset
        if isinstance(self.gpu_memory_free_bytes, Unset):
            gpu_memory_free_bytes = UNSET
        else:
            gpu_memory_free_bytes = self.gpu_memory_free_bytes

        gpu_memory_total_bytes: int | None | Unset
        if isinstance(self.gpu_memory_total_bytes, Unset):
            gpu_memory_total_bytes = UNSET
        else:
            gpu_memory_total_bytes = self.gpu_memory_total_bytes

        gpu_temperature_c: int | None | Unset
        if isinstance(self.gpu_temperature_c, Unset):
            gpu_temperature_c = UNSET
        else:
            gpu_temperature_c = self.gpu_temperature_c

        gpu_unavailable_reason: None | str | Unset
        if isinstance(self.gpu_unavailable_reason, Unset):
            gpu_unavailable_reason = UNSET
        elif isinstance(self.gpu_unavailable_reason, str):
            gpu_unavailable_reason = self.gpu_unavailable_reason
        else:
            gpu_unavailable_reason = self.gpu_unavailable_reason

        gpu_utilization_percent: float | None | Unset
        if isinstance(self.gpu_utilization_percent, Unset):
            gpu_utilization_percent = UNSET
        else:
            gpu_utilization_percent = self.gpu_utilization_percent

        memory_available_bytes: int | None | Unset
        if isinstance(self.memory_available_bytes, Unset):
            memory_available_bytes = UNSET
        else:
            memory_available_bytes = self.memory_available_bytes

        memory_total_bytes: int | None | Unset
        if isinstance(self.memory_total_bytes, Unset):
            memory_total_bytes = UNSET
        else:
            memory_total_bytes = self.memory_total_bytes

        renewal_failed: bool | None | Unset
        if isinstance(self.renewal_failed, Unset):
            renewal_failed = UNSET
        else:
            renewal_failed = self.renewal_failed


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "boot_id": boot_id,
            "id": id,
            "node_id": node_id,
            "observed_at": observed_at,
            "received_at": received_at,
        })
        if cpu_frequency_avg_mhz is not UNSET:
            field_dict["cpu_frequency_avg_mhz"] = cpu_frequency_avg_mhz
        if cpu_frequency_max_mhz is not UNSET:
            field_dict["cpu_frequency_max_mhz"] = cpu_frequency_max_mhz
        if cpu_frequency_min_mhz is not UNSET:
            field_dict["cpu_frequency_min_mhz"] = cpu_frequency_min_mhz
        if credential_remaining_fraction is not UNSET:
            field_dict["credential_remaining_fraction"] = credential_remaining_fraction
        if disk_free_bytes is not UNSET:
            field_dict["disk_free_bytes"] = disk_free_bytes
        if disk_total_bytes is not UNSET:
            field_dict["disk_total_bytes"] = disk_total_bytes
        if gpu_memory_free_bytes is not UNSET:
            field_dict["gpu_memory_free_bytes"] = gpu_memory_free_bytes
        if gpu_memory_total_bytes is not UNSET:
            field_dict["gpu_memory_total_bytes"] = gpu_memory_total_bytes
        if gpu_temperature_c is not UNSET:
            field_dict["gpu_temperature_c"] = gpu_temperature_c
        if gpu_unavailable_reason is not UNSET:
            field_dict["gpu_unavailable_reason"] = gpu_unavailable_reason
        if gpu_utilization_percent is not UNSET:
            field_dict["gpu_utilization_percent"] = gpu_utilization_percent
        if memory_available_bytes is not UNSET:
            field_dict["memory_available_bytes"] = memory_available_bytes
        if memory_total_bytes is not UNSET:
            field_dict["memory_total_bytes"] = memory_total_bytes
        if renewal_failed is not UNSET:
            field_dict["renewal_failed"] = renewal_failed

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        boot_id = d.pop("boot_id")

        id = d.pop("id")

        node_id = d.pop("node_id")

        observed_at = datetime.datetime.fromisoformat(d.pop("observed_at"))




        received_at = datetime.datetime.fromisoformat(d.pop("received_at"))




        def _parse_cpu_frequency_avg_mhz(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        cpu_frequency_avg_mhz = _parse_cpu_frequency_avg_mhz(d.pop("cpu_frequency_avg_mhz", UNSET))


        def _parse_cpu_frequency_max_mhz(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        cpu_frequency_max_mhz = _parse_cpu_frequency_max_mhz(d.pop("cpu_frequency_max_mhz", UNSET))


        def _parse_cpu_frequency_min_mhz(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        cpu_frequency_min_mhz = _parse_cpu_frequency_min_mhz(d.pop("cpu_frequency_min_mhz", UNSET))


        def _parse_credential_remaining_fraction(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        credential_remaining_fraction = _parse_credential_remaining_fraction(d.pop("credential_remaining_fraction", UNSET))


        def _parse_disk_free_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        disk_free_bytes = _parse_disk_free_bytes(d.pop("disk_free_bytes", UNSET))


        def _parse_disk_total_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        disk_total_bytes = _parse_disk_total_bytes(d.pop("disk_total_bytes", UNSET))


        def _parse_gpu_memory_free_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        gpu_memory_free_bytes = _parse_gpu_memory_free_bytes(d.pop("gpu_memory_free_bytes", UNSET))


        def _parse_gpu_memory_total_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        gpu_memory_total_bytes = _parse_gpu_memory_total_bytes(d.pop("gpu_memory_total_bytes", UNSET))


        def _parse_gpu_temperature_c(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        gpu_temperature_c = _parse_gpu_temperature_c(d.pop("gpu_temperature_c", UNSET))


        def _parse_gpu_unavailable_reason(data: object) -> GpuUnavailableReason | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                gpu_unavailable_reason_type_0 = check_gpu_unavailable_reason(data)



                return gpu_unavailable_reason_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(GpuUnavailableReason | None | Unset, data)

        gpu_unavailable_reason = _parse_gpu_unavailable_reason(d.pop("gpu_unavailable_reason", UNSET))


        def _parse_gpu_utilization_percent(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        gpu_utilization_percent = _parse_gpu_utilization_percent(d.pop("gpu_utilization_percent", UNSET))


        def _parse_memory_available_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        memory_available_bytes = _parse_memory_available_bytes(d.pop("memory_available_bytes", UNSET))


        def _parse_memory_total_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        memory_total_bytes = _parse_memory_total_bytes(d.pop("memory_total_bytes", UNSET))


        def _parse_renewal_failed(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        renewal_failed = _parse_renewal_failed(d.pop("renewal_failed", UNSET))


        telemetry_point = cls(
            boot_id=boot_id,
            id=id,
            node_id=node_id,
            observed_at=observed_at,
            received_at=received_at,
            cpu_frequency_avg_mhz=cpu_frequency_avg_mhz,
            cpu_frequency_max_mhz=cpu_frequency_max_mhz,
            cpu_frequency_min_mhz=cpu_frequency_min_mhz,
            credential_remaining_fraction=credential_remaining_fraction,
            disk_free_bytes=disk_free_bytes,
            disk_total_bytes=disk_total_bytes,
            gpu_memory_free_bytes=gpu_memory_free_bytes,
            gpu_memory_total_bytes=gpu_memory_total_bytes,
            gpu_temperature_c=gpu_temperature_c,
            gpu_unavailable_reason=gpu_unavailable_reason,
            gpu_utilization_percent=gpu_utilization_percent,
            memory_available_bytes=memory_available_bytes,
            memory_total_bytes=memory_total_bytes,
            renewal_failed=renewal_failed,
        )

        return telemetry_point
