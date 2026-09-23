from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_resource_requirement_memory_kind_type_0 import check_fleet_profile_resource_requirement_memory_kind_type_0
from ..models.fleet_profile_resource_requirement_memory_kind_type_0 import FleetProfileResourceRequirementMemoryKindType0
from ..models.fleet_profile_resource_requirement_memory_pool_type_0 import check_fleet_profile_resource_requirement_memory_pool_type_0
from ..models.fleet_profile_resource_requirement_memory_pool_type_0 import FleetProfileResourceRequirementMemoryPoolType0
from typing import cast
from typing import cast, Union

if TYPE_CHECKING:
  from ..models.resource_demand_evidence import ResourceDemandEvidence





T = TypeVar("T", bound="FleetProfileResourceRequirement")



@_attrs_define
class FleetProfileResourceRequirement:
    """ Stable demand and eligibility, separate from observed free capacity.

        Attributes:
            allowed (bool):
            disk_required_bytes (Union[None, int]):
            memory_floor_bytes (Union[None, int]):
            memory_kind (Union[FleetProfileResourceRequirementMemoryKindType0, None]):
            memory_pool (Union[FleetProfileResourceRequirementMemoryPoolType0, None]):
            memory_required_bytes (Union[None, int]):
            node_id (str):
            ports_required (list[int]):
            resource_demand (Union['ResourceDemandEvidence', None]):
     """

    allowed: bool
    disk_required_bytes: Union[None, int]
    memory_floor_bytes: Union[None, int]
    memory_kind: Union[FleetProfileResourceRequirementMemoryKindType0, None]
    memory_pool: Union[FleetProfileResourceRequirementMemoryPoolType0, None]
    memory_required_bytes: Union[None, int]
    node_id: str
    ports_required: list[int]
    resource_demand: Union['ResourceDemandEvidence', None]





    def to_dict(self) -> dict[str, Any]:
        from ..models.resource_demand_evidence import ResourceDemandEvidence
        allowed = self.allowed

        disk_required_bytes: Union[None, int]
        disk_required_bytes = self.disk_required_bytes

        memory_floor_bytes: Union[None, int]
        memory_floor_bytes = self.memory_floor_bytes

        memory_kind: Union[None, str]
        if isinstance(self.memory_kind, str):
            memory_kind = self.memory_kind
        else:
            memory_kind = self.memory_kind

        memory_pool: Union[None, str]
        if isinstance(self.memory_pool, str):
            memory_pool = self.memory_pool
        else:
            memory_pool = self.memory_pool

        memory_required_bytes: Union[None, int]
        memory_required_bytes = self.memory_required_bytes

        node_id = self.node_id

        ports_required = self.ports_required



        resource_demand: Union[None, dict[str, Any]]
        if isinstance(self.resource_demand, ResourceDemandEvidence):
            resource_demand = self.resource_demand.to_dict()
        else:
            resource_demand = self.resource_demand


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "allowed": allowed,
            "disk_required_bytes": disk_required_bytes,
            "memory_floor_bytes": memory_floor_bytes,
            "memory_kind": memory_kind,
            "memory_pool": memory_pool,
            "memory_required_bytes": memory_required_bytes,
            "node_id": node_id,
            "ports_required": ports_required,
            "resource_demand": resource_demand,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.resource_demand_evidence import ResourceDemandEvidence
        d = dict(src_dict)
        allowed = d.pop("allowed")

        def _parse_disk_required_bytes(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        disk_required_bytes = _parse_disk_required_bytes(d.pop("disk_required_bytes"))


        def _parse_memory_floor_bytes(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        memory_floor_bytes = _parse_memory_floor_bytes(d.pop("memory_floor_bytes"))


        def _parse_memory_kind(data: object) -> Union[FleetProfileResourceRequirementMemoryKindType0, None]:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                memory_kind_type_0 = check_fleet_profile_resource_requirement_memory_kind_type_0(data)



                return memory_kind_type_0
            except: # noqa: E722
                pass
            return cast(Union[FleetProfileResourceRequirementMemoryKindType0, None], data)

        memory_kind = _parse_memory_kind(d.pop("memory_kind"))


        def _parse_memory_pool(data: object) -> Union[FleetProfileResourceRequirementMemoryPoolType0, None]:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                memory_pool_type_0 = check_fleet_profile_resource_requirement_memory_pool_type_0(data)



                return memory_pool_type_0
            except: # noqa: E722
                pass
            return cast(Union[FleetProfileResourceRequirementMemoryPoolType0, None], data)

        memory_pool = _parse_memory_pool(d.pop("memory_pool"))


        def _parse_memory_required_bytes(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        memory_required_bytes = _parse_memory_required_bytes(d.pop("memory_required_bytes"))


        node_id = d.pop("node_id")

        ports_required = cast(list[int], d.pop("ports_required"))


        def _parse_resource_demand(data: object) -> Union['ResourceDemandEvidence', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                resource_demand_type_0 = ResourceDemandEvidence.from_dict(data)



                return resource_demand_type_0
            except: # noqa: E722
                pass
            return cast(Union['ResourceDemandEvidence', None], data)

        resource_demand = _parse_resource_demand(d.pop("resource_demand"))


        fleet_profile_resource_requirement = cls(
            allowed=allowed,
            disk_required_bytes=disk_required_bytes,
            memory_floor_bytes=memory_floor_bytes,
            memory_kind=memory_kind,
            memory_pool=memory_pool,
            memory_required_bytes=memory_required_bytes,
            node_id=node_id,
            ports_required=ports_required,
            resource_demand=resource_demand,
        )

        return fleet_profile_resource_requirement
