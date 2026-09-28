from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

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
            disk_free_bytes (int | None | Unset):
            disk_total_bytes (int | None | Unset):
            gpu_memory_free_bytes (int | None | Unset):
            gpu_memory_total_bytes (int | None | Unset):
            gpu_utilization_percent (float | None | Unset):
            memory_available_bytes (int | None | Unset):
            memory_total_bytes (int | None | Unset):
     """

    boot_id: str
    id: str
    node_id: str
    observed_at: datetime.datetime
    received_at: datetime.datetime
    disk_free_bytes: int | None | Unset = UNSET
    disk_total_bytes: int | None | Unset = UNSET
    gpu_memory_free_bytes: int | None | Unset = UNSET
    gpu_memory_total_bytes: int | None | Unset = UNSET
    gpu_utilization_percent: float | None | Unset = UNSET
    memory_available_bytes: int | None | Unset = UNSET
    memory_total_bytes: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        boot_id = self.boot_id

        id = self.id

        node_id = self.node_id

        observed_at = self.observed_at.isoformat()

        received_at = self.received_at.isoformat()

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


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "boot_id": boot_id,
            "id": id,
            "node_id": node_id,
            "observed_at": observed_at,
            "received_at": received_at,
        })
        if disk_free_bytes is not UNSET:
            field_dict["disk_free_bytes"] = disk_free_bytes
        if disk_total_bytes is not UNSET:
            field_dict["disk_total_bytes"] = disk_total_bytes
        if gpu_memory_free_bytes is not UNSET:
            field_dict["gpu_memory_free_bytes"] = gpu_memory_free_bytes
        if gpu_memory_total_bytes is not UNSET:
            field_dict["gpu_memory_total_bytes"] = gpu_memory_total_bytes
        if gpu_utilization_percent is not UNSET:
            field_dict["gpu_utilization_percent"] = gpu_utilization_percent
        if memory_available_bytes is not UNSET:
            field_dict["memory_available_bytes"] = memory_available_bytes
        if memory_total_bytes is not UNSET:
            field_dict["memory_total_bytes"] = memory_total_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        boot_id = d.pop("boot_id")

        id = d.pop("id")

        node_id = d.pop("node_id")

        observed_at = datetime.datetime.fromisoformat(d.pop("observed_at"))




        received_at = datetime.datetime.fromisoformat(d.pop("received_at"))




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


        telemetry_point = cls(
            boot_id=boot_id,
            id=id,
            node_id=node_id,
            observed_at=observed_at,
            received_at=received_at,
            disk_free_bytes=disk_free_bytes,
            disk_total_bytes=disk_total_bytes,
            gpu_memory_free_bytes=gpu_memory_free_bytes,
            gpu_memory_total_bytes=gpu_memory_total_bytes,
            gpu_utilization_percent=gpu_utilization_percent,
            memory_available_bytes=memory_available_bytes,
            memory_total_bytes=memory_total_bytes,
        )

        return telemetry_point
