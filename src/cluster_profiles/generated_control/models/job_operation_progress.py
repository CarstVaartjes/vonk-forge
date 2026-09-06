from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.operation_checkpoint import OperationCheckpoint
  from ..models.operation_member_progress import OperationMemberProgress





T = TypeVar("T", bound="JobOperationProgress")



@_attrs_define
class JobOperationProgress:
    """
        Attributes:
            phase (str):
            bytes_per_second (Union[None, Unset, float]):
            checkpoint (Union['OperationCheckpoint', None, Unset]):
            completed_bytes (Union[None, Unset, int]):
            eta_seconds (Union[None, Unset, float]):
            members (Union[None, Unset, list['OperationMemberProgress']]):
            total_bytes (Union[None, Unset, int]):
            total_bytes_known (Union[None, Unset, bool]):
     """

    phase: str
    bytes_per_second: Union[None, Unset, float] = UNSET
    checkpoint: Union['OperationCheckpoint', None, Unset] = UNSET
    completed_bytes: Union[None, Unset, int] = UNSET
    eta_seconds: Union[None, Unset, float] = UNSET
    members: Union[None, Unset, list['OperationMemberProgress']] = UNSET
    total_bytes: Union[None, Unset, int] = UNSET
    total_bytes_known: Union[None, Unset, bool] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.operation_checkpoint import OperationCheckpoint
        from ..models.operation_member_progress import OperationMemberProgress
        phase = self.phase

        bytes_per_second: Union[None, Unset, float]
        if isinstance(self.bytes_per_second, Unset):
            bytes_per_second = UNSET
        else:
            bytes_per_second = self.bytes_per_second

        checkpoint: Union[None, Unset, dict[str, Any]]
        if isinstance(self.checkpoint, Unset):
            checkpoint = UNSET
        elif isinstance(self.checkpoint, OperationCheckpoint):
            checkpoint = self.checkpoint.to_dict()
        else:
            checkpoint = self.checkpoint

        completed_bytes: Union[None, Unset, int]
        if isinstance(self.completed_bytes, Unset):
            completed_bytes = UNSET
        else:
            completed_bytes = self.completed_bytes

        eta_seconds: Union[None, Unset, float]
        if isinstance(self.eta_seconds, Unset):
            eta_seconds = UNSET
        else:
            eta_seconds = self.eta_seconds

        members: Union[None, Unset, list[dict[str, Any]]]
        if isinstance(self.members, Unset):
            members = UNSET
        elif isinstance(self.members, list):
            members = []
            for members_type_0_item_data in self.members:
                members_type_0_item = members_type_0_item_data.to_dict()
                members.append(members_type_0_item)


        else:
            members = self.members

        total_bytes: Union[None, Unset, int]
        if isinstance(self.total_bytes, Unset):
            total_bytes = UNSET
        else:
            total_bytes = self.total_bytes

        total_bytes_known: Union[None, Unset, bool]
        if isinstance(self.total_bytes_known, Unset):
            total_bytes_known = UNSET
        else:
            total_bytes_known = self.total_bytes_known


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "phase": phase,
        })
        if bytes_per_second is not UNSET:
            field_dict["bytes_per_second"] = bytes_per_second
        if checkpoint is not UNSET:
            field_dict["checkpoint"] = checkpoint
        if completed_bytes is not UNSET:
            field_dict["completed_bytes"] = completed_bytes
        if eta_seconds is not UNSET:
            field_dict["eta_seconds"] = eta_seconds
        if members is not UNSET:
            field_dict["members"] = members
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes
        if total_bytes_known is not UNSET:
            field_dict["total_bytes_known"] = total_bytes_known

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operation_checkpoint import OperationCheckpoint
        from ..models.operation_member_progress import OperationMemberProgress
        d = dict(src_dict)
        phase = d.pop("phase")

        def _parse_bytes_per_second(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        bytes_per_second = _parse_bytes_per_second(d.pop("bytes_per_second", UNSET))


        def _parse_checkpoint(data: object) -> Union['OperationCheckpoint', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                checkpoint_type_0 = OperationCheckpoint.from_dict(data)



                return checkpoint_type_0
            except: # noqa: E722
                pass
            return cast(Union['OperationCheckpoint', None, Unset], data)

        checkpoint = _parse_checkpoint(d.pop("checkpoint", UNSET))


        def _parse_completed_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        completed_bytes = _parse_completed_bytes(d.pop("completed_bytes", UNSET))


        def _parse_eta_seconds(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        eta_seconds = _parse_eta_seconds(d.pop("eta_seconds", UNSET))


        def _parse_members(data: object) -> Union[None, Unset, list['OperationMemberProgress']]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                members_type_0 = []
                _members_type_0 = data
                for members_type_0_item_data in (_members_type_0):
                    members_type_0_item = OperationMemberProgress.from_dict(members_type_0_item_data)



                    members_type_0.append(members_type_0_item)

                return members_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, Unset, list['OperationMemberProgress']], data)

        members = _parse_members(d.pop("members", UNSET))


        def _parse_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        def _parse_total_bytes_known(data: object) -> Union[None, Unset, bool]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, bool], data)

        total_bytes_known = _parse_total_bytes_known(d.pop("total_bytes_known", UNSET))


        job_operation_progress = cls(
            phase=phase,
            bytes_per_second=bytes_per_second,
            checkpoint=checkpoint,
            completed_bytes=completed_bytes,
            eta_seconds=eta_seconds,
            members=members,
            total_bytes=total_bytes,
            total_bytes_known=total_bytes_known,
        )

        return job_operation_progress
