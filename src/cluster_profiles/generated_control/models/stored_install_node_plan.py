from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
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
            blockers (list[StoredAdmissionReason]):
            disk_floor_bytes (int):
            free_after_bytes (int | None):
            free_bytes (int | None):
            inventory_observed_at (datetime.datetime | None):
            node_id (str):
            rank (int):
            required_bytes (int):
            required_download_bytes (int):
            reused_bytes (int):
            role (str):
            warnings (list[StoredAdmissionReason]):
            required_payload_bytes (int | None | Unset):
     """

    active_reserved_bytes: int
    allowed: bool
    blockers: list[StoredAdmissionReason]
    disk_floor_bytes: int
    free_after_bytes: int | None
    free_bytes: int | None
    inventory_observed_at: datetime.datetime | None
    node_id: str
    rank: int
    required_bytes: int
    required_download_bytes: int
    reused_bytes: int
    role: str
    warnings: list[StoredAdmissionReason]
    required_payload_bytes: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.stored_admission_reason import StoredAdmissionReason # noqa: PLC0415
        active_reserved_bytes = self.active_reserved_bytes

        allowed = self.allowed

        blockers = []
        for blockers_item_data in self.blockers:
            blockers_item = blockers_item_data.to_dict()
            blockers.append(blockers_item)



        disk_floor_bytes = self.disk_floor_bytes

        free_after_bytes: int | None
        free_after_bytes = self.free_after_bytes

        free_bytes: int | None
        free_bytes = self.free_bytes

        inventory_observed_at: None | str
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



        required_payload_bytes: int | None | Unset
        if isinstance(self.required_payload_bytes, Unset):
            required_payload_bytes = UNSET
        else:
            required_payload_bytes = self.required_payload_bytes


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
        if required_payload_bytes is not UNSET:
            field_dict["required_payload_bytes"] = required_payload_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.stored_admission_reason import StoredAdmissionReason # noqa: PLC0415
        d = dict(src_dict)
        active_reserved_bytes = d.pop("active_reserved_bytes")

        allowed = d.pop("allowed")

        blockers = []
        _blockers = d.pop("blockers")
        for blockers_item_data in (_blockers):
            blockers_item = StoredAdmissionReason.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        disk_floor_bytes = d.pop("disk_floor_bytes")

        def _parse_free_after_bytes(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        free_after_bytes = _parse_free_after_bytes(d.pop("free_after_bytes"))


        def _parse_free_bytes(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        free_bytes = _parse_free_bytes(d.pop("free_bytes"))


        def _parse_inventory_observed_at(data: object) -> datetime.datetime | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                inventory_observed_at_type_0 = datetime.datetime.fromisoformat(data)



                return inventory_observed_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None, data)

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


        def _parse_required_payload_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        required_payload_bytes = _parse_required_payload_bytes(d.pop("required_payload_bytes", UNSET))


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
            required_payload_bytes=required_payload_bytes,
        )

        return stored_install_node_plan
