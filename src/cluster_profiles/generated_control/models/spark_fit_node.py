from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.spark_fit_node_memory_kind_type_0 import check_spark_fit_node_memory_kind_type_0
from ..models.spark_fit_node_memory_kind_type_0 import SparkFitNodeMemoryKindType0
from ..models.spark_fit_node_memory_pool_type_0 import check_spark_fit_node_memory_pool_type_0
from ..models.spark_fit_node_memory_pool_type_0 import SparkFitNodeMemoryPoolType0
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.run_switch_reason import RunSwitchReason
  from ..models.resource_demand_evidence import ResourceDemandEvidence
  from ..models.memory_usage_uncertainty import MemoryUsageUncertainty





T = TypeVar("T", bound="SparkFitNode")



@_attrs_define
class SparkFitNode:
    """
        Attributes:
            allowed (bool):
            node_id (str):
            ports_required (list[int]):
            rank (int):
            role (str):
            blockers (Union[Unset, list['RunSwitchReason']]):
            disk_free_after_bytes (Union[None, Unset, int]):
            disk_free_bytes (Union[None, Unset, int]):
            disk_required_bytes (Union[None, Unset, int]):
            memory_available_bytes (Union[None, Unset, int]):
            memory_capacity_bytes (Union[None, Unset, int]):
            memory_floor_bytes (Union[None, Unset, int]):
            memory_free_after_bytes (Union[None, Unset, int]):
            memory_kind (Union[None, SparkFitNodeMemoryKindType0, Unset]):
            memory_pool (Union[None, SparkFitNodeMemoryPoolType0, Unset]):
            memory_required_bytes (Union[None, Unset, int]):
            memory_usage_uncertainty (Union['MemoryUsageUncertainty', None, Unset]):
            resource_demand (Union['ResourceDemandEvidence', None, Unset]):
            warnings (Union[Unset, list['RunSwitchReason']]):
     """

    allowed: bool
    node_id: str
    ports_required: list[int]
    rank: int
    role: str
    blockers: Union[Unset, list['RunSwitchReason']] = UNSET
    disk_free_after_bytes: Union[None, Unset, int] = UNSET
    disk_free_bytes: Union[None, Unset, int] = UNSET
    disk_required_bytes: Union[None, Unset, int] = UNSET
    memory_available_bytes: Union[None, Unset, int] = UNSET
    memory_capacity_bytes: Union[None, Unset, int] = UNSET
    memory_floor_bytes: Union[None, Unset, int] = UNSET
    memory_free_after_bytes: Union[None, Unset, int] = UNSET
    memory_kind: Union[None, SparkFitNodeMemoryKindType0, Unset] = UNSET
    memory_pool: Union[None, SparkFitNodeMemoryPoolType0, Unset] = UNSET
    memory_required_bytes: Union[None, Unset, int] = UNSET
    memory_usage_uncertainty: Union['MemoryUsageUncertainty', None, Unset] = UNSET
    resource_demand: Union['ResourceDemandEvidence', None, Unset] = UNSET
    warnings: Union[Unset, list['RunSwitchReason']] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_reason import RunSwitchReason
        from ..models.resource_demand_evidence import ResourceDemandEvidence
        from ..models.memory_usage_uncertainty import MemoryUsageUncertainty
        allowed = self.allowed

        node_id = self.node_id

        ports_required = self.ports_required



        rank = self.rank

        role = self.role

        blockers: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.blockers, Unset):
            blockers = []
            for blockers_item_data in self.blockers:
                blockers_item = blockers_item_data.to_dict()
                blockers.append(blockers_item)



        disk_free_after_bytes: Union[None, Unset, int]
        if isinstance(self.disk_free_after_bytes, Unset):
            disk_free_after_bytes = UNSET
        else:
            disk_free_after_bytes = self.disk_free_after_bytes

        disk_free_bytes: Union[None, Unset, int]
        if isinstance(self.disk_free_bytes, Unset):
            disk_free_bytes = UNSET
        else:
            disk_free_bytes = self.disk_free_bytes

        disk_required_bytes: Union[None, Unset, int]
        if isinstance(self.disk_required_bytes, Unset):
            disk_required_bytes = UNSET
        else:
            disk_required_bytes = self.disk_required_bytes

        memory_available_bytes: Union[None, Unset, int]
        if isinstance(self.memory_available_bytes, Unset):
            memory_available_bytes = UNSET
        else:
            memory_available_bytes = self.memory_available_bytes

        memory_capacity_bytes: Union[None, Unset, int]
        if isinstance(self.memory_capacity_bytes, Unset):
            memory_capacity_bytes = UNSET
        else:
            memory_capacity_bytes = self.memory_capacity_bytes

        memory_floor_bytes: Union[None, Unset, int]
        if isinstance(self.memory_floor_bytes, Unset):
            memory_floor_bytes = UNSET
        else:
            memory_floor_bytes = self.memory_floor_bytes

        memory_free_after_bytes: Union[None, Unset, int]
        if isinstance(self.memory_free_after_bytes, Unset):
            memory_free_after_bytes = UNSET
        else:
            memory_free_after_bytes = self.memory_free_after_bytes

        memory_kind: Union[None, Unset, str]
        if isinstance(self.memory_kind, Unset):
            memory_kind = UNSET
        elif isinstance(self.memory_kind, str):
            memory_kind = self.memory_kind
        else:
            memory_kind = self.memory_kind

        memory_pool: Union[None, Unset, str]
        if isinstance(self.memory_pool, Unset):
            memory_pool = UNSET
        elif isinstance(self.memory_pool, str):
            memory_pool = self.memory_pool
        else:
            memory_pool = self.memory_pool

        memory_required_bytes: Union[None, Unset, int]
        if isinstance(self.memory_required_bytes, Unset):
            memory_required_bytes = UNSET
        else:
            memory_required_bytes = self.memory_required_bytes

        memory_usage_uncertainty: Union[None, Unset, dict[str, Any]]
        if isinstance(self.memory_usage_uncertainty, Unset):
            memory_usage_uncertainty = UNSET
        elif isinstance(self.memory_usage_uncertainty, MemoryUsageUncertainty):
            memory_usage_uncertainty = self.memory_usage_uncertainty.to_dict()
        else:
            memory_usage_uncertainty = self.memory_usage_uncertainty

        resource_demand: Union[None, Unset, dict[str, Any]]
        if isinstance(self.resource_demand, Unset):
            resource_demand = UNSET
        elif isinstance(self.resource_demand, ResourceDemandEvidence):
            resource_demand = self.resource_demand.to_dict()
        else:
            resource_demand = self.resource_demand

        warnings: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.warnings, Unset):
            warnings = []
            for warnings_item_data in self.warnings:
                warnings_item = warnings_item_data.to_dict()
                warnings.append(warnings_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "allowed": allowed,
            "node_id": node_id,
            "ports_required": ports_required,
            "rank": rank,
            "role": role,
        })
        if blockers is not UNSET:
            field_dict["blockers"] = blockers
        if disk_free_after_bytes is not UNSET:
            field_dict["disk_free_after_bytes"] = disk_free_after_bytes
        if disk_free_bytes is not UNSET:
            field_dict["disk_free_bytes"] = disk_free_bytes
        if disk_required_bytes is not UNSET:
            field_dict["disk_required_bytes"] = disk_required_bytes
        if memory_available_bytes is not UNSET:
            field_dict["memory_available_bytes"] = memory_available_bytes
        if memory_capacity_bytes is not UNSET:
            field_dict["memory_capacity_bytes"] = memory_capacity_bytes
        if memory_floor_bytes is not UNSET:
            field_dict["memory_floor_bytes"] = memory_floor_bytes
        if memory_free_after_bytes is not UNSET:
            field_dict["memory_free_after_bytes"] = memory_free_after_bytes
        if memory_kind is not UNSET:
            field_dict["memory_kind"] = memory_kind
        if memory_pool is not UNSET:
            field_dict["memory_pool"] = memory_pool
        if memory_required_bytes is not UNSET:
            field_dict["memory_required_bytes"] = memory_required_bytes
        if memory_usage_uncertainty is not UNSET:
            field_dict["memory_usage_uncertainty"] = memory_usage_uncertainty
        if resource_demand is not UNSET:
            field_dict["resource_demand"] = resource_demand
        if warnings is not UNSET:
            field_dict["warnings"] = warnings

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_reason import RunSwitchReason
        from ..models.resource_demand_evidence import ResourceDemandEvidence
        from ..models.memory_usage_uncertainty import MemoryUsageUncertainty
        d = dict(src_dict)
        allowed = d.pop("allowed")

        node_id = d.pop("node_id")

        ports_required = cast(list[int], d.pop("ports_required"))


        rank = d.pop("rank")

        role = d.pop("role")

        blockers = []
        _blockers = d.pop("blockers", UNSET)
        for blockers_item_data in (_blockers or []):
            blockers_item = RunSwitchReason.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        def _parse_disk_free_after_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        disk_free_after_bytes = _parse_disk_free_after_bytes(d.pop("disk_free_after_bytes", UNSET))


        def _parse_disk_free_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        disk_free_bytes = _parse_disk_free_bytes(d.pop("disk_free_bytes", UNSET))


        def _parse_disk_required_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        disk_required_bytes = _parse_disk_required_bytes(d.pop("disk_required_bytes", UNSET))


        def _parse_memory_available_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        memory_available_bytes = _parse_memory_available_bytes(d.pop("memory_available_bytes", UNSET))


        def _parse_memory_capacity_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        memory_capacity_bytes = _parse_memory_capacity_bytes(d.pop("memory_capacity_bytes", UNSET))


        def _parse_memory_floor_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        memory_floor_bytes = _parse_memory_floor_bytes(d.pop("memory_floor_bytes", UNSET))


        def _parse_memory_free_after_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        memory_free_after_bytes = _parse_memory_free_after_bytes(d.pop("memory_free_after_bytes", UNSET))


        def _parse_memory_kind(data: object) -> Union[None, SparkFitNodeMemoryKindType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                memory_kind_type_0 = check_spark_fit_node_memory_kind_type_0(data)



                return memory_kind_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, SparkFitNodeMemoryKindType0, Unset], data)

        memory_kind = _parse_memory_kind(d.pop("memory_kind", UNSET))


        def _parse_memory_pool(data: object) -> Union[None, SparkFitNodeMemoryPoolType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                memory_pool_type_0 = check_spark_fit_node_memory_pool_type_0(data)



                return memory_pool_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, SparkFitNodeMemoryPoolType0, Unset], data)

        memory_pool = _parse_memory_pool(d.pop("memory_pool", UNSET))


        def _parse_memory_required_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        memory_required_bytes = _parse_memory_required_bytes(d.pop("memory_required_bytes", UNSET))


        def _parse_memory_usage_uncertainty(data: object) -> Union['MemoryUsageUncertainty', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                memory_usage_uncertainty_type_0 = MemoryUsageUncertainty.from_dict(data)



                return memory_usage_uncertainty_type_0
            except: # noqa: E722
                pass
            return cast(Union['MemoryUsageUncertainty', None, Unset], data)

        memory_usage_uncertainty = _parse_memory_usage_uncertainty(d.pop("memory_usage_uncertainty", UNSET))


        def _parse_resource_demand(data: object) -> Union['ResourceDemandEvidence', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                resource_demand_type_0 = ResourceDemandEvidence.from_dict(data)



                return resource_demand_type_0
            except: # noqa: E722
                pass
            return cast(Union['ResourceDemandEvidence', None, Unset], data)

        resource_demand = _parse_resource_demand(d.pop("resource_demand", UNSET))


        warnings = []
        _warnings = d.pop("warnings", UNSET)
        for warnings_item_data in (_warnings or []):
            warnings_item = RunSwitchReason.from_dict(warnings_item_data)



            warnings.append(warnings_item)


        spark_fit_node = cls(
            allowed=allowed,
            node_id=node_id,
            ports_required=ports_required,
            rank=rank,
            role=role,
            blockers=blockers,
            disk_free_after_bytes=disk_free_after_bytes,
            disk_free_bytes=disk_free_bytes,
            disk_required_bytes=disk_required_bytes,
            memory_available_bytes=memory_available_bytes,
            memory_capacity_bytes=memory_capacity_bytes,
            memory_floor_bytes=memory_floor_bytes,
            memory_free_after_bytes=memory_free_after_bytes,
            memory_kind=memory_kind,
            memory_pool=memory_pool,
            memory_required_bytes=memory_required_bytes,
            memory_usage_uncertainty=memory_usage_uncertainty,
            resource_demand=resource_demand,
            warnings=warnings,
        )

        return spark_fit_node
