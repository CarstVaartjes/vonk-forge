from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.inventory_state_freshness import check_inventory_state_freshness
from ..models.inventory_state_freshness import InventoryStateFreshness
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime






T = TypeVar("T", bound="InventoryState")



@_attrs_define
class InventoryState:
    """
        Attributes:
            age_seconds (float):
            artifact_store_read_only (bool):
            capabilities (list[str]):
            container_runtime_version (str):
            disk_free_bytes (int):
            disk_total_bytes (int):
            freshness (InventoryStateFreshness):
            gpu_count (int):
            gpu_memory_free_bytes (int):
            gpu_memory_total_bytes (int):
            host_memory_free_bytes (int):
            host_memory_total_bytes (int):
            nvidia_driver_version (str):
            observed_at (datetime.datetime):
            received_at (datetime.datetime):
            fabric_address (Union[None, Unset, str]):
            fabric_bandwidth_mbps (Union[None, Unset, int]):
     """

    age_seconds: float
    artifact_store_read_only: bool
    capabilities: list[str]
    container_runtime_version: str
    disk_free_bytes: int
    disk_total_bytes: int
    freshness: InventoryStateFreshness
    gpu_count: int
    gpu_memory_free_bytes: int
    gpu_memory_total_bytes: int
    host_memory_free_bytes: int
    host_memory_total_bytes: int
    nvidia_driver_version: str
    observed_at: datetime.datetime
    received_at: datetime.datetime
    fabric_address: Union[None, Unset, str] = UNSET
    fabric_bandwidth_mbps: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        age_seconds = self.age_seconds

        artifact_store_read_only = self.artifact_store_read_only

        capabilities = self.capabilities



        container_runtime_version = self.container_runtime_version

        disk_free_bytes = self.disk_free_bytes

        disk_total_bytes = self.disk_total_bytes

        freshness: str = self.freshness

        gpu_count = self.gpu_count

        gpu_memory_free_bytes = self.gpu_memory_free_bytes

        gpu_memory_total_bytes = self.gpu_memory_total_bytes

        host_memory_free_bytes = self.host_memory_free_bytes

        host_memory_total_bytes = self.host_memory_total_bytes

        nvidia_driver_version = self.nvidia_driver_version

        observed_at = self.observed_at.isoformat()

        received_at = self.received_at.isoformat()

        fabric_address: Union[None, Unset, str]
        if isinstance(self.fabric_address, Unset):
            fabric_address = UNSET
        else:
            fabric_address = self.fabric_address

        fabric_bandwidth_mbps: Union[None, Unset, int]
        if isinstance(self.fabric_bandwidth_mbps, Unset):
            fabric_bandwidth_mbps = UNSET
        else:
            fabric_bandwidth_mbps = self.fabric_bandwidth_mbps


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "age_seconds": age_seconds,
            "artifact_store_read_only": artifact_store_read_only,
            "capabilities": capabilities,
            "container_runtime_version": container_runtime_version,
            "disk_free_bytes": disk_free_bytes,
            "disk_total_bytes": disk_total_bytes,
            "freshness": freshness,
            "gpu_count": gpu_count,
            "gpu_memory_free_bytes": gpu_memory_free_bytes,
            "gpu_memory_total_bytes": gpu_memory_total_bytes,
            "host_memory_free_bytes": host_memory_free_bytes,
            "host_memory_total_bytes": host_memory_total_bytes,
            "nvidia_driver_version": nvidia_driver_version,
            "observed_at": observed_at,
            "received_at": received_at,
        })
        if fabric_address is not UNSET:
            field_dict["fabric_address"] = fabric_address
        if fabric_bandwidth_mbps is not UNSET:
            field_dict["fabric_bandwidth_mbps"] = fabric_bandwidth_mbps

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        age_seconds = d.pop("age_seconds")

        artifact_store_read_only = d.pop("artifact_store_read_only")

        capabilities = cast(list[str], d.pop("capabilities"))


        container_runtime_version = d.pop("container_runtime_version")

        disk_free_bytes = d.pop("disk_free_bytes")

        disk_total_bytes = d.pop("disk_total_bytes")

        freshness = check_inventory_state_freshness(d.pop("freshness"))




        gpu_count = d.pop("gpu_count")

        gpu_memory_free_bytes = d.pop("gpu_memory_free_bytes")

        gpu_memory_total_bytes = d.pop("gpu_memory_total_bytes")

        host_memory_free_bytes = d.pop("host_memory_free_bytes")

        host_memory_total_bytes = d.pop("host_memory_total_bytes")

        nvidia_driver_version = d.pop("nvidia_driver_version")

        observed_at = isoparse(d.pop("observed_at"))




        received_at = isoparse(d.pop("received_at"))




        def _parse_fabric_address(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        fabric_address = _parse_fabric_address(d.pop("fabric_address", UNSET))


        def _parse_fabric_bandwidth_mbps(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        fabric_bandwidth_mbps = _parse_fabric_bandwidth_mbps(d.pop("fabric_bandwidth_mbps", UNSET))


        inventory_state = cls(
            age_seconds=age_seconds,
            artifact_store_read_only=artifact_store_read_only,
            capabilities=capabilities,
            container_runtime_version=container_runtime_version,
            disk_free_bytes=disk_free_bytes,
            disk_total_bytes=disk_total_bytes,
            freshness=freshness,
            gpu_count=gpu_count,
            gpu_memory_free_bytes=gpu_memory_free_bytes,
            gpu_memory_total_bytes=gpu_memory_total_bytes,
            host_memory_free_bytes=host_memory_free_bytes,
            host_memory_total_bytes=host_memory_total_bytes,
            nvidia_driver_version=nvidia_driver_version,
            observed_at=observed_at,
            received_at=received_at,
            fabric_address=fabric_address,
            fabric_bandwidth_mbps=fabric_bandwidth_mbps,
        )

        return inventory_state
