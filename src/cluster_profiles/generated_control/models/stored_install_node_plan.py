from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
import datetime

if TYPE_CHECKING:
  from ..models.stored_admission_reason import StoredAdmissionReason





T = TypeVar("T", bound="StoredInstallNodePlan")



@_attrs_define
class StoredInstallNodePlan:
    """
        Attributes:
            active_reserved_bytes (int):
            allowed (bool):
            blockers (list['StoredAdmissionReason']):
            disk_floor_bytes (int):
            free_after_bytes (Union[None, int]):
            free_bytes (Union[None, int]):
            inventory_observed_at (Union[None, datetime.datetime]):
            node_id (str):
            rank (int):
            required_bytes (int):
            required_download_bytes (int):
            reused_bytes (int):
            role (str):
            warnings (list['StoredAdmissionReason']):
     """

    active_reserved_bytes: int
    allowed: bool
    blockers: list['StoredAdmissionReason']
    disk_floor_bytes: int
    free_after_bytes: Union[None, int]
    free_bytes: Union[None, int]
    inventory_observed_at: Union[None, datetime.datetime]
    node_id: str
    rank: int
    required_bytes: int
    required_download_bytes: int
    reused_bytes: int
    role: str
    warnings: list['StoredAdmissionReason']





    def to_dict(self) -> dict[str, Any]:
        from ..models.stored_admission_reason import StoredAdmissionReason
        active_reserved_bytes = self.active_reserved_bytes

        allowed = self.allowed

        blockers = []
        for blockers_item_data in self.blockers:
            blockers_item = blockers_item_data.to_dict()
            blockers.append(blockers_item)



        disk_floor_bytes = self.disk_floor_bytes

        free_after_bytes: Union[None, int]
        free_after_bytes = self.free_after_bytes

        free_bytes: Union[None, int]
        free_bytes = self.free_bytes

        inventory_observed_at: Union[None, str]
        if isinstance(self.inventory_observed_at, datetime.datetime):
            inventory_observed_at = self.inventory_observed_at.isoformat()
        else:
            inventory_observed_at = self.inventory_observed_at

        node_id = self.node_id

        rank = self.rank

        required_bytes = self.required_bytes

        required_download_bytes = self.required_download_bytes

        reused_bytes = self.reused_bytes

        role = self.role

        warnings = []
        for warnings_item_data in self.warnings:
            warnings_item = warnings_item_data.to_dict()
            warnings.append(warnings_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "active_reserved_bytes": active_reserved_bytes,
            "allowed": allowed,
            "blockers": blockers,
            "disk_floor_bytes": disk_floor_bytes,
            "free_after_bytes": free_after_bytes,
            "free_bytes": free_bytes,
            "inventory_observed_at": inventory_observed_at,
            "node_id": node_id,
            "rank": rank,
            "required_bytes": required_bytes,
            "required_download_bytes": required_download_bytes,
            "reused_bytes": reused_bytes,
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

        blockers = []
        _blockers = d.pop("blockers")
        for blockers_item_data in (_blockers):
            blockers_item = StoredAdmissionReason.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        disk_floor_bytes = d.pop("disk_floor_bytes")

        def _parse_free_after_bytes(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        free_after_bytes = _parse_free_after_bytes(d.pop("free_after_bytes"))


        def _parse_free_bytes(data: object) -> Union[None, int]:
            if data is None:
                return data
            return cast(Union[None, int], data)

        free_bytes = _parse_free_bytes(d.pop("free_bytes"))


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


        node_id = d.pop("node_id")

        rank = d.pop("rank")

        required_bytes = d.pop("required_bytes")

        required_download_bytes = d.pop("required_download_bytes")

        reused_bytes = d.pop("reused_bytes")

        role = d.pop("role")

        warnings = []
        _warnings = d.pop("warnings")
        for warnings_item_data in (_warnings):
            warnings_item = StoredAdmissionReason.from_dict(warnings_item_data)



            warnings.append(warnings_item)


        stored_install_node_plan = cls(
            active_reserved_bytes=active_reserved_bytes,
            allowed=allowed,
            blockers=blockers,
            disk_floor_bytes=disk_floor_bytes,
            free_after_bytes=free_after_bytes,
            free_bytes=free_bytes,
            inventory_observed_at=inventory_observed_at,
            node_id=node_id,
            rank=rank,
            required_bytes=required_bytes,
            required_download_bytes=required_download_bytes,
            reused_bytes=reused_bytes,
            role=role,
            warnings=warnings,
        )

        return stored_install_node_plan
