from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.stored_run_node_plan_memory_kind import check_stored_run_node_plan_memory_kind
from ..models.stored_run_node_plan_memory_kind import StoredRunNodePlanMemoryKind
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
import datetime

if TYPE_CHECKING:
  from ..models.stored_admission_reason import StoredAdmissionReason





T = TypeVar("T", bound="StoredRunNodePlan")



@_attrs_define
class StoredRunNodePlan:
    """
        Attributes:
            active_reserved_bytes (int):
            allowed (bool):
            available_memory_bytes (Union[None, int]):
            blockers (list['StoredAdmissionReason']):
            endpoint_owner (bool):
            fabric_address (Union[None, str]):
            fabric_bandwidth_mbps (Union[None, int]):
            free_after_bytes (Union[None, int]):
            inventory_observed_at (Union[None, datetime.datetime]):
            memory_floor_bytes (int):
            memory_kind (StoredRunNodePlanMemoryKind):
            node_id (str):
            port (int):
            rank (int):
            rendezvous_port (Union[None, int]):
            required_memory_bytes (int):
            role (str):
            warnings (list['StoredAdmissionReason']):
     """

    active_reserved_bytes: int
    allowed: bool
    available_memory_bytes: Union[None, int]
    blockers: list['StoredAdmissionReason']
    endpoint_owner: bool
    fabric_address: Union[None, str]
    fabric_bandwidth_mbps: Union[None, int]
    free_after_bytes: Union[None, int]
    inventory_observed_at: Union[None, datetime.datetime]
    memory_floor_bytes: int
    memory_kind: StoredRunNodePlanMemoryKind
    node_id: str
    port: int
    rank: int
    rendezvous_port: Union[None, int]
    required_memory_bytes: int
    role: str
    warnings: list['StoredAdmissionReason']





    def to_dict(self) -> dict[str, Any]:
        from ..models.stored_admission_reason import StoredAdmissionReason
        active_reserved_bytes = self.active_reserved_bytes

        allowed = self.allowed

        available_memory_bytes: Union[None, int]
        available_memory_bytes = self.available_memory_bytes

        blockers = []
        for blockers_item_data in self.blockers:
            blockers_item = blockers_item_data.to_dict()
            blockers.append(blockers_item)



        endpoint_owner = self.endpoint_owner

        fabric_address: Union[None, str]
        fabric_address = self.fabric_address

        fabric_bandwidth_mbps: Union[None, int]
        fabric_bandwidth_mbps = self.fabric_bandwidth_mbps

        free_after_bytes: Union[None, int]
        free_after_bytes = self.free_after_bytes

        inventory_observed_at: Union[None, str]
        if isinstance(self.inventory_observed_at, datetime.datetime):
            inventory_observed_at = self.inventory_observed_at.isoformat()
        else:
            inventory_observed_at = self.inventory_observed_at

        memory_floor_bytes = self.memory_floor_bytes

        memory_kind: str = self.memory_kind

        node_id = self.node_id

        port = self.port

        rank = self.rank

        rendezvous_port: Union[None, int]
        rendezvous_port = self.rendezvous_port

        required_memory_bytes = self.required_memory_bytes

        role = self.role

        warnings = []
        for warnings_item_data in self.warnings:
            warnings_item = warnings_item_data.to_dict()
            warnings.append(warnings_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "active_reserved_bytes": active_reserved_bytes,
            "allowed": allowed,
            "available_memory_bytes": available_memory_bytes,
            "blockers": blockers,
            "endpoint_owner": endpoint_owner,
            "fabric_address": fabric_address,
            "fabric_bandwidth_mbps": fabric_bandwidth_mbps,
            "free_after_bytes": free_after_bytes,
            "inventory_observed_at": inventory_observed_at,
            "memory_floor_bytes": memory_floor_bytes,
            "memory_kind": memory_kind,
            "node_id": node_id,
            "port": port,
            "rank": rank,
            "rendezvous_port": rendezvous_port,
            "required_memory_bytes": required_memory_bytes,
            "role": role,
            "warnings": warnings,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.stored_admission_reason import StoredAdmissionReason
        d = dict(src_dict)
        active_reserved_bytes = d.pop("active_reserved_bytes")

        allowed = d.pop("allowed")

        def _parse_available_memory_bytes(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        available_memory_bytes = _parse_available_memory_bytes(d.pop("available_memory_bytes"))


        blockers = []
        _blockers = d.pop("blockers")
        for blockers_item_data in (_blockers):
            blockers_item = StoredAdmissionReason.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        endpoint_owner = d.pop("endpoint_owner")

        def _parse_fabric_address(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        fabric_address = _parse_fabric_address(d.pop("fabric_address"))


        def _parse_fabric_bandwidth_mbps(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        fabric_bandwidth_mbps = _parse_fabric_bandwidth_mbps(d.pop("fabric_bandwidth_mbps"))


        def _parse_free_after_bytes(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        free_after_bytes = _parse_free_after_bytes(d.pop("free_after_bytes"))


        def _parse_inventory_observed_at(data: object) -> Union[None, datetime.datetime]:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                inventory_observed_at_type_0 = isoparse(data)



                return inventory_observed_at_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, datetime.datetime], data)

        inventory_observed_at = _parse_inventory_observed_at(d.pop("inventory_observed_at"))


        memory_floor_bytes = d.pop("memory_floor_bytes")

        memory_kind = check_stored_run_node_plan_memory_kind(d.pop("memory_kind"))




        node_id = d.pop("node_id")

        port = d.pop("port")

        rank = d.pop("rank")

        def _parse_rendezvous_port(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        rendezvous_port = _parse_rendezvous_port(d.pop("rendezvous_port"))


        required_memory_bytes = d.pop("required_memory_bytes")

        role = d.pop("role")

        warnings = []
        _warnings = d.pop("warnings")
        for warnings_item_data in (_warnings):
            warnings_item = StoredAdmissionReason.from_dict(warnings_item_data)



            warnings.append(warnings_item)


        stored_run_node_plan = cls(
            active_reserved_bytes=active_reserved_bytes,
            allowed=allowed,
            available_memory_bytes=available_memory_bytes,
            blockers=blockers,
            endpoint_owner=endpoint_owner,
            fabric_address=fabric_address,
            fabric_bandwidth_mbps=fabric_bandwidth_mbps,
            free_after_bytes=free_after_bytes,
            inventory_observed_at=inventory_observed_at,
            memory_floor_bytes=memory_floor_bytes,
            memory_kind=memory_kind,
            node_id=node_id,
            port=port,
            rank=rank,
            rendezvous_port=rendezvous_port,
            required_memory_bytes=required_memory_bytes,
            role=role,
            warnings=warnings,
        )

        return stored_run_node_plan
