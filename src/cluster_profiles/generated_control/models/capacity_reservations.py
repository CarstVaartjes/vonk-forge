from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="CapacityReservations")



@_attrs_define
class CapacityReservations:
    """
        Attributes:
            disk_bytes (int):
            gpu_memory_bytes (int):
            host_memory_bytes (int):
            port_count (int):
            unified_memory_bytes (int):
     """

    disk_bytes: int
    gpu_memory_bytes: int
    host_memory_bytes: int
    port_count: int
    unified_memory_bytes: int





    def to_dict(self) -> dict[str, Any]:
        disk_bytes = self.disk_bytes

        gpu_memory_bytes = self.gpu_memory_bytes

        host_memory_bytes = self.host_memory_bytes

        port_count = self.port_count

        unified_memory_bytes = self.unified_memory_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "disk_bytes": disk_bytes,
            "gpu_memory_bytes": gpu_memory_bytes,
            "host_memory_bytes": host_memory_bytes,
            "port_count": port_count,
            "unified_memory_bytes": unified_memory_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        disk_bytes = d.pop("disk_bytes")

        gpu_memory_bytes = d.pop("gpu_memory_bytes")

        host_memory_bytes = d.pop("host_memory_bytes")

        port_count = d.pop("port_count")

        unified_memory_bytes = d.pop("unified_memory_bytes")

        capacity_reservations = cls(
            disk_bytes=disk_bytes,
            gpu_memory_bytes=gpu_memory_bytes,
            host_memory_bytes=host_memory_bytes,
            port_count=port_count,
            unified_memory_bytes=unified_memory_bytes,
        )

        return capacity_reservations
