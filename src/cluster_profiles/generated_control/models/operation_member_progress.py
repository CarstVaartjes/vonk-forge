from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="OperationMemberProgress")



@_attrs_define
class OperationMemberProgress:
    """ Progress for one node, rank, shard, or other operation member.

        Attributes:
            member_id (str):
            phase (str):
            bytes_per_second (Union[None, Unset, float]):
            completed_bytes (Union[Unset, int]):  Default: 0.
            eta_seconds (Union[None, Unset, float]):
            state (Union[Unset, str]):  Default: 'running'.
            total_bytes (Union[None, Unset, int]):
     """

    member_id: str
    phase: str
    bytes_per_second: Union[None, Unset, float] = UNSET
    completed_bytes: Union[Unset, int] = 0
    eta_seconds: Union[None, Unset, float] = UNSET
    state: Union[Unset, str] = 'running'
    total_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        member_id = self.member_id

        phase = self.phase

        bytes_per_second: Union[None, Unset, float]
        if isinstance(self.bytes_per_second, Unset):
            bytes_per_second = UNSET
        else:
            bytes_per_second = self.bytes_per_second

        completed_bytes = self.completed_bytes

        eta_seconds: Union[None, Unset, float]
        if isinstance(self.eta_seconds, Unset):
            eta_seconds = UNSET
        else:
            eta_seconds = self.eta_seconds

        state = self.state

        total_bytes: Union[None, Unset, int]
        if isinstance(self.total_bytes, Unset):
            total_bytes = UNSET
        else:
            total_bytes = self.total_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "member_id": member_id,
            "phase": phase,
        })
        if bytes_per_second is not UNSET:
            field_dict["bytes_per_second"] = bytes_per_second
        if completed_bytes is not UNSET:
            field_dict["completed_bytes"] = completed_bytes
        if eta_seconds is not UNSET:
            field_dict["eta_seconds"] = eta_seconds
        if state is not UNSET:
            field_dict["state"] = state
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        member_id = d.pop("member_id")

        phase = d.pop("phase")

        def _parse_bytes_per_second(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        bytes_per_second = _parse_bytes_per_second(d.pop("bytes_per_second", UNSET))


        completed_bytes = d.pop("completed_bytes", UNSET)

        def _parse_eta_seconds(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        eta_seconds = _parse_eta_seconds(d.pop("eta_seconds", UNSET))


        state = d.pop("state", UNSET)

        def _parse_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        operation_member_progress = cls(
            member_id=member_id,
            phase=phase,
            bytes_per_second=bytes_per_second,
            completed_bytes=completed_bytes,
            eta_seconds=eta_seconds,
            state=state,
            total_bytes=total_bytes,
        )

        return operation_member_progress
