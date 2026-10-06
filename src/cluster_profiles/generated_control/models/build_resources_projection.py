from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="BuildResourcesProjection")



@_attrs_define
class BuildResourcesProjection:
    """
        Attributes:
            cpu_cores (int):
            download_bytes (int):
            memory_bytes (int):
            processes (int):
            temporary_bytes (int):
            timeout_seconds (int):
     """

    cpu_cores: int
    download_bytes: int
    memory_bytes: int
    processes: int
    temporary_bytes: int
    timeout_seconds: int





    def to_dict(self) -> dict[str, Any]:
        cpu_cores = self.cpu_cores

        download_bytes = self.download_bytes

        memory_bytes = self.memory_bytes

        processes = self.processes

        temporary_bytes = self.temporary_bytes

        timeout_seconds = self.timeout_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "cpu_cores": cpu_cores,
            "download_bytes": download_bytes,
            "memory_bytes": memory_bytes,
            "processes": processes,
            "temporary_bytes": temporary_bytes,
            "timeout_seconds": timeout_seconds,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        cpu_cores = d.pop("cpu_cores")

        download_bytes = d.pop("download_bytes")

        memory_bytes = d.pop("memory_bytes")

        processes = d.pop("processes")

        temporary_bytes = d.pop("temporary_bytes")

        timeout_seconds = d.pop("timeout_seconds")

        build_resources_projection = cls(
            cpu_cores=cpu_cores,
            download_bytes=download_bytes,
            memory_bytes=memory_bytes,
            processes=processes,
            temporary_bytes=temporary_bytes,
            timeout_seconds=timeout_seconds,
        )

        return build_resources_projection
