from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.operation_member_progress_activity_type_0 import check_operation_member_progress_activity_type_0
from ..models.operation_member_progress_activity_type_0 import OperationMemberProgressActivityType0
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="OperationMemberProgress")



@_attrs_define
class OperationMemberProgress:
    """ Progress for one node, rank, shard, or other operation member.

        Attributes:
            member_id (str):
            phase (str):
            activity (Union[None, OperationMemberProgressActivityType0, Unset]):
            bytes_per_second (Union[None, Unset, float]):
            completed_bytes (Union[Unset, int]):  Default: 0.
            completed_items (Union[None, Unset, int]):
            elapsed_seconds (Union[None, Unset, float]):
            eta_seconds (Union[None, Unset, float]):
            kind (Union[None, Unset, str]):
            last_progress_at (Union[None, Unset, str]):
            object_sha256 (Union[None, Unset, str]):
            observed_at (Union[None, Unset, str]):
            smoothed_bytes_per_second (Union[None, Unset, float]):
            state (Union[Unset, str]):  Default: 'running'.
            total_bytes (Union[None, Unset, int]):
            total_items (Union[None, Unset, int]):
     """

    member_id: str
    phase: str
    activity: Union[None, OperationMemberProgressActivityType0, Unset] = UNSET
    bytes_per_second: Union[None, Unset, float] = UNSET
    completed_bytes: Union[Unset, int] = 0
    completed_items: Union[None, Unset, int] = UNSET
    elapsed_seconds: Union[None, Unset, float] = UNSET
    eta_seconds: Union[None, Unset, float] = UNSET
    kind: Union[None, Unset, str] = UNSET
    last_progress_at: Union[None, Unset, str] = UNSET
    object_sha256: Union[None, Unset, str] = UNSET
    observed_at: Union[None, Unset, str] = UNSET
    smoothed_bytes_per_second: Union[None, Unset, float] = UNSET
    state: Union[Unset, str] = 'running'
    total_bytes: Union[None, Unset, int] = UNSET
    total_items: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        member_id = self.member_id

        phase = self.phase

        activity: Union[None, Unset, str]
        if isinstance(self.activity, Unset):
            activity = UNSET
        elif isinstance(self.activity, str):
            activity = self.activity
        else:
            activity = self.activity

        bytes_per_second: Union[None, Unset, float]
        if isinstance(self.bytes_per_second, Unset):
            bytes_per_second = UNSET
        else:
            bytes_per_second = self.bytes_per_second

        completed_bytes = self.completed_bytes

        completed_items: Union[None, Unset, int]
        if isinstance(self.completed_items, Unset):
            completed_items = UNSET
        else:
            completed_items = self.completed_items

        elapsed_seconds: Union[None, Unset, float]
        if isinstance(self.elapsed_seconds, Unset):
            elapsed_seconds = UNSET
        else:
            elapsed_seconds = self.elapsed_seconds

        eta_seconds: Union[None, Unset, float]
        if isinstance(self.eta_seconds, Unset):
            eta_seconds = UNSET
        else:
            eta_seconds = self.eta_seconds

        kind: Union[None, Unset, str]
        if isinstance(self.kind, Unset):
            kind = UNSET
        else:
            kind = self.kind

        last_progress_at: Union[None, Unset, str]
        if isinstance(self.last_progress_at, Unset):
            last_progress_at = UNSET
        else:
            last_progress_at = self.last_progress_at

        object_sha256: Union[None, Unset, str]
        if isinstance(self.object_sha256, Unset):
            object_sha256 = UNSET
        else:
            object_sha256 = self.object_sha256

        observed_at: Union[None, Unset, str]
        if isinstance(self.observed_at, Unset):
            observed_at = UNSET
        else:
            observed_at = self.observed_at

        smoothed_bytes_per_second: Union[None, Unset, float]
        if isinstance(self.smoothed_bytes_per_second, Unset):
            smoothed_bytes_per_second = UNSET
        else:
            smoothed_bytes_per_second = self.smoothed_bytes_per_second

        state = self.state

        total_bytes: Union[None, Unset, int]
        if isinstance(self.total_bytes, Unset):
            total_bytes = UNSET
        else:
            total_bytes = self.total_bytes

        total_items: Union[None, Unset, int]
        if isinstance(self.total_items, Unset):
            total_items = UNSET
        else:
            total_items = self.total_items


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "member_id": member_id,
            "phase": phase,
        })
        if activity is not UNSET:
            field_dict["activity"] = activity
        if bytes_per_second is not UNSET:
            field_dict["bytes_per_second"] = bytes_per_second
        if completed_bytes is not UNSET:
            field_dict["completed_bytes"] = completed_bytes
        if completed_items is not UNSET:
            field_dict["completed_items"] = completed_items
        if elapsed_seconds is not UNSET:
            field_dict["elapsed_seconds"] = elapsed_seconds
        if eta_seconds is not UNSET:
            field_dict["eta_seconds"] = eta_seconds
        if kind is not UNSET:
            field_dict["kind"] = kind
        if last_progress_at is not UNSET:
            field_dict["last_progress_at"] = last_progress_at
        if object_sha256 is not UNSET:
            field_dict["object_sha256"] = object_sha256
        if observed_at is not UNSET:
            field_dict["observed_at"] = observed_at
        if smoothed_bytes_per_second is not UNSET:
            field_dict["smoothed_bytes_per_second"] = smoothed_bytes_per_second
        if state is not UNSET:
            field_dict["state"] = state
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes
        if total_items is not UNSET:
            field_dict["total_items"] = total_items

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        member_id = d.pop("member_id")

        phase = d.pop("phase")

        def _parse_activity(data: object) -> Union[None, OperationMemberProgressActivityType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                activity_type_0 = check_operation_member_progress_activity_type_0(data)



                return activity_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, OperationMemberProgressActivityType0, Unset], data)

        activity = _parse_activity(d.pop("activity", UNSET))


        def _parse_bytes_per_second(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        bytes_per_second = _parse_bytes_per_second(d.pop("bytes_per_second", UNSET))


        completed_bytes = d.pop("completed_bytes", UNSET)

        def _parse_completed_items(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        completed_items = _parse_completed_items(d.pop("completed_items", UNSET))


        def _parse_elapsed_seconds(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        elapsed_seconds = _parse_elapsed_seconds(d.pop("elapsed_seconds", UNSET))


        def _parse_eta_seconds(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        eta_seconds = _parse_eta_seconds(d.pop("eta_seconds", UNSET))


        def _parse_kind(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        kind = _parse_kind(d.pop("kind", UNSET))


        def _parse_last_progress_at(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        last_progress_at = _parse_last_progress_at(d.pop("last_progress_at", UNSET))


        def _parse_object_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        object_sha256 = _parse_object_sha256(d.pop("object_sha256", UNSET))


        def _parse_observed_at(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        observed_at = _parse_observed_at(d.pop("observed_at", UNSET))


        def _parse_smoothed_bytes_per_second(data: object) -> Union[None, Unset, float]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, float], data)

        smoothed_bytes_per_second = _parse_smoothed_bytes_per_second(d.pop("smoothed_bytes_per_second", UNSET))


        state = d.pop("state", UNSET)

        def _parse_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        def _parse_total_items(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total_items = _parse_total_items(d.pop("total_items", UNSET))


        operation_member_progress = cls(
            member_id=member_id,
            phase=phase,
            activity=activity,
            bytes_per_second=bytes_per_second,
            completed_bytes=completed_bytes,
            completed_items=completed_items,
            elapsed_seconds=elapsed_seconds,
            eta_seconds=eta_seconds,
            kind=kind,
            last_progress_at=last_progress_at,
            object_sha256=object_sha256,
            observed_at=observed_at,
            smoothed_bytes_per_second=smoothed_bytes_per_second,
            state=state,
            total_bytes=total_bytes,
            total_items=total_items,
        )

        return operation_member_progress
