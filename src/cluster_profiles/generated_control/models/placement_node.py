from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.placement_node_memory_kind import check_placement_node_memory_kind
from ..models.placement_node_memory_kind import PlacementNodeMemoryKind
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime






T = TypeVar("T", bound="PlacementNode")



@_attrs_define
class PlacementNode:
    """
        Attributes:
            artifact_reuse_bytes (int):
            disk_free_after_bytes (int):
            disk_free_bytes (int):
            disk_required_bytes (int):
            disk_reserved_bytes (int):
            endpoint_owner (bool):
            fabric_address (Union[None, str]):
            inventory_age_seconds (float):
            inventory_observed_at (datetime.datetime):
            memory_available_bytes (int):
            memory_free_after_bytes (int):
            memory_kind (PlacementNodeMemoryKind):
            memory_required_bytes (int):
            memory_reserved_bytes (int):
            node_id (str):
            rank (int):
            role (str):
            telemetry_age_seconds (float):
            telemetry_observed_at (datetime.datetime):
            fabric_bandwidth_mbps (Union[None, Unset, int]):
     """

    artifact_reuse_bytes: int
    disk_free_after_bytes: int
    disk_free_bytes: int
    disk_required_bytes: int
    disk_reserved_bytes: int
    endpoint_owner: bool
    fabric_address: Union[None, str]
    inventory_age_seconds: float
    inventory_observed_at: datetime.datetime
    memory_available_bytes: int
    memory_free_after_bytes: int
    memory_kind: PlacementNodeMemoryKind
    memory_required_bytes: int
    memory_reserved_bytes: int
    node_id: str
    rank: int
    role: str
    telemetry_age_seconds: float
    telemetry_observed_at: datetime.datetime
    fabric_bandwidth_mbps: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        artifact_reuse_bytes = self.artifact_reuse_bytes

        disk_free_after_bytes = self.disk_free_after_bytes

        disk_free_bytes = self.disk_free_bytes

        disk_required_bytes = self.disk_required_bytes

        disk_reserved_bytes = self.disk_reserved_bytes

        endpoint_owner = self.endpoint_owner

        fabric_address: Union[None, str]
        fabric_address = self.fabric_address

        inventory_age_seconds = self.inventory_age_seconds

        inventory_observed_at = self.inventory_observed_at.isoformat()

        memory_available_bytes = self.memory_available_bytes

        memory_free_after_bytes = self.memory_free_after_bytes

        memory_kind: str = self.memory_kind

        memory_required_bytes = self.memory_required_bytes

        memory_reserved_bytes = self.memory_reserved_bytes

        node_id = self.node_id

        rank = self.rank

        role = self.role

        telemetry_age_seconds = self.telemetry_age_seconds

        telemetry_observed_at = self.telemetry_observed_at.isoformat()

        fabric_bandwidth_mbps: Union[None, Unset, int]
        if isinstance(self.fabric_bandwidth_mbps, Unset):
            fabric_bandwidth_mbps = UNSET
        else:
            fabric_bandwidth_mbps = self.fabric_bandwidth_mbps


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_reuse_bytes": artifact_reuse_bytes,
            "disk_free_after_bytes": disk_free_after_bytes,
            "disk_free_bytes": disk_free_bytes,
            "disk_required_bytes": disk_required_bytes,
            "disk_reserved_bytes": disk_reserved_bytes,
            "endpoint_owner": endpoint_owner,
            "fabric_address": fabric_address,
            "inventory_age_seconds": inventory_age_seconds,
            "inventory_observed_at": inventory_observed_at,
            "memory_available_bytes": memory_available_bytes,
            "memory_free_after_bytes": memory_free_after_bytes,
            "memory_kind": memory_kind,
            "memory_required_bytes": memory_required_bytes,
            "memory_reserved_bytes": memory_reserved_bytes,
            "node_id": node_id,
            "rank": rank,
            "role": role,
            "telemetry_age_seconds": telemetry_age_seconds,
            "telemetry_observed_at": telemetry_observed_at,
        })
        if fabric_bandwidth_mbps is not UNSET:
            field_dict["fabric_bandwidth_mbps"] = fabric_bandwidth_mbps

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_reuse_bytes = d.pop("artifact_reuse_bytes")

        disk_free_after_bytes = d.pop("disk_free_after_bytes")

        disk_free_bytes = d.pop("disk_free_bytes")

        disk_required_bytes = d.pop("disk_required_bytes")

        disk_reserved_bytes = d.pop("disk_reserved_bytes")

        endpoint_owner = d.pop("endpoint_owner")

        def _parse_fabric_address(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        fabric_address = _parse_fabric_address(d.pop("fabric_address"))


        inventory_age_seconds = d.pop("inventory_age_seconds")

        inventory_observed_at = isoparse(d.pop("inventory_observed_at"))




        memory_available_bytes = d.pop("memory_available_bytes")

        memory_free_after_bytes = d.pop("memory_free_after_bytes")

        memory_kind = check_placement_node_memory_kind(d.pop("memory_kind"))




        memory_required_bytes = d.pop("memory_required_bytes")

        memory_reserved_bytes = d.pop("memory_reserved_bytes")

        node_id = d.pop("node_id")

        rank = d.pop("rank")

        role = d.pop("role")

        telemetry_age_seconds = d.pop("telemetry_age_seconds")

        telemetry_observed_at = isoparse(d.pop("telemetry_observed_at"))




        def _parse_fabric_bandwidth_mbps(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        fabric_bandwidth_mbps = _parse_fabric_bandwidth_mbps(d.pop("fabric_bandwidth_mbps", UNSET))


        placement_node = cls(
            artifact_reuse_bytes=artifact_reuse_bytes,
            disk_free_after_bytes=disk_free_after_bytes,
            disk_free_bytes=disk_free_bytes,
            disk_required_bytes=disk_required_bytes,
            disk_reserved_bytes=disk_reserved_bytes,
            endpoint_owner=endpoint_owner,
            fabric_address=fabric_address,
            inventory_age_seconds=inventory_age_seconds,
            inventory_observed_at=inventory_observed_at,
            memory_available_bytes=memory_available_bytes,
            memory_free_after_bytes=memory_free_after_bytes,
            memory_kind=memory_kind,
            memory_required_bytes=memory_required_bytes,
            memory_reserved_bytes=memory_reserved_bytes,
            node_id=node_id,
            rank=rank,
            role=role,
            telemetry_age_seconds=telemetry_age_seconds,
            telemetry_observed_at=telemetry_observed_at,
            fabric_bandwidth_mbps=fabric_bandwidth_mbps,
        )

        return placement_node
