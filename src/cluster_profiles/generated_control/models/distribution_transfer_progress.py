from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.distribution_transfer_progress_phase import check_distribution_transfer_progress_phase
from ..models.distribution_transfer_progress_phase import DistributionTransferProgressPhase
from typing import cast

if TYPE_CHECKING:
  from ..models.run_switch_member_receipt import RunSwitchMemberReceipt





T = TypeVar("T", bound="DistributionTransferProgress")



@_attrs_define
class DistributionTransferProgress:
    """
        Attributes:
            completed_bytes (int):
            members (list[RunSwitchMemberReceipt]):
            phase (DistributionTransferProgressPhase):
            total_bytes (int):
            total_bytes_known (bool):
     """

    completed_bytes: int
    members: list[RunSwitchMemberReceipt]
    phase: DistributionTransferProgressPhase
    total_bytes: int
    total_bytes_known: bool





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_member_receipt import RunSwitchMemberReceipt # noqa: PLC0415
        completed_bytes = self.completed_bytes

        members = []
        for members_item_data in self.members:
            members_item = members_item_data.to_dict()
            members.append(members_item)



        phase: str = self.phase

        total_bytes = self.total_bytes

        total_bytes_known = self.total_bytes_known


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "completed_bytes": completed_bytes,
            "members": members,
            "phase": phase,
            "total_bytes": total_bytes,
            "total_bytes_known": total_bytes_known,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_member_receipt import RunSwitchMemberReceipt # noqa: PLC0415
        d = dict(src_dict)
        completed_bytes = d.pop("completed_bytes")

        members = []
        _members = d.pop("members")
        for members_item_data in (_members):
            members_item = RunSwitchMemberReceipt.from_dict(members_item_data)



            members.append(members_item)


        phase = check_distribution_transfer_progress_phase(d.pop("phase"))




        total_bytes = d.pop("total_bytes")

        total_bytes_known = d.pop("total_bytes_known")

        distribution_transfer_progress = cls(
            completed_bytes=completed_bytes,
            members=members,
            phase=phase,
            total_bytes=total_bytes,
            total_bytes_known=total_bytes_known,
        )

        return distribution_transfer_progress
