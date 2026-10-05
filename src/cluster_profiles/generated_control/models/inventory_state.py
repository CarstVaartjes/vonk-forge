from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.inventory_state_freshness import check_inventory_state_freshness
from ..models.inventory_state_freshness import InventoryStateFreshness
from ..types import UNSET, Unset
from typing import cast
import datetime

if TYPE_CHECKING:
  from ..models.network_interface import NetworkInterface





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
            fabric_address (None | str | Unset):
            fabric_bandwidth_mbps (int | None | Unset):
            nas_route_interface (None | str | Unset):
            network_interfaces (list[NetworkInterface] | None | Unset):
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
    fabric_address: None | str | Unset = UNSET
    fabric_bandwidth_mbps: int | None | Unset = UNSET
    nas_route_interface: None | str | Unset = UNSET
    network_interfaces: list[NetworkInterface] | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.network_interface import NetworkInterface # noqa: PLC0415
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

        fabric_address: None | str | Unset
        if isinstance(self.fabric_address, Unset):
            fabric_address = UNSET
        else:
            fabric_address = self.fabric_address

        fabric_bandwidth_mbps: int | None | Unset
        if isinstance(self.fabric_bandwidth_mbps, Unset):
            fabric_bandwidth_mbps = UNSET
        else:
            fabric_bandwidth_mbps = self.fabric_bandwidth_mbps

        nas_route_interface: None | str | Unset
        if isinstance(self.nas_route_interface, Unset):
            nas_route_interface = UNSET
        else:
            nas_route_interface = self.nas_route_interface

        network_interfaces: list[dict[str, Any]] | None | Unset
        if isinstance(self.network_interfaces, Unset):
            network_interfaces = UNSET
        elif isinstance(self.network_interfaces, list):
            network_interfaces = []
            for network_interfaces_type_0_item_data in self.network_interfaces:
                network_interfaces_type_0_item = network_interfaces_type_0_item_data.to_dict()
                network_interfaces.append(network_interfaces_type_0_item)


        else:
            network_interfaces = self.network_interfaces


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
        if nas_route_interface is not UNSET:
            field_dict["nas_route_interface"] = nas_route_interface
        if network_interfaces is not UNSET:
            field_dict["network_interfaces"] = network_interfaces

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.network_interface import NetworkInterface # noqa: PLC0415
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

        observed_at = datetime.datetime.fromisoformat(d.pop("observed_at"))




        received_at = datetime.datetime.fromisoformat(d.pop("received_at"))




        def _parse_fabric_address(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        fabric_address = _parse_fabric_address(d.pop("fabric_address", UNSET))


        def _parse_fabric_bandwidth_mbps(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        fabric_bandwidth_mbps = _parse_fabric_bandwidth_mbps(d.pop("fabric_bandwidth_mbps", UNSET))


        def _parse_nas_route_interface(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        nas_route_interface = _parse_nas_route_interface(d.pop("nas_route_interface", UNSET))


        def _parse_network_interfaces(data: object) -> list[NetworkInterface] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                network_interfaces_type_0 = []
                _network_interfaces_type_0 = data
                for network_interfaces_type_0_item_data in (_network_interfaces_type_0):
                    network_interfaces_type_0_item = NetworkInterface.from_dict(network_interfaces_type_0_item_data)



                    network_interfaces_type_0.append(network_interfaces_type_0_item)

                return network_interfaces_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[NetworkInterface] | None | Unset, data)

        network_interfaces = _parse_network_interfaces(d.pop("network_interfaces", UNSET))


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
            nas_route_interface=nas_route_interface,
            network_interfaces=network_interfaces,
        )

        return inventory_state
