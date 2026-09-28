from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_child_progress_phase_type_0 import check_run_switch_child_progress_phase_type_0
from ..models.run_switch_child_progress_phase_type_0 import RunSwitchChildProgressPhaseType0
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.operation_progress import OperationProgress
  from ..models.run_switch_member_receipt import RunSwitchMemberReceipt





T = TypeVar("T", bound="RunSwitchChildProgress")



@_attrs_define
class RunSwitchChildProgress:
    """ Progress nested in a durable child receipt.

        Attributes:
            completed_bytes (int | Unset):  Default: 0.
            members (list[RunSwitchMemberReceipt] | Unset):
            operation (None | OperationProgress | Unset):
            phase (None | RunSwitchChildProgressPhaseType0 | Unset):
            total_bytes (int | None | Unset):
            total_bytes_known (bool | Unset):  Default: False.
     """

    completed_bytes: int | Unset = 0
    members: list[RunSwitchMemberReceipt] | Unset = UNSET
    operation: None | OperationProgress | Unset = UNSET
    phase: None | RunSwitchChildProgressPhaseType0 | Unset = UNSET
    total_bytes: int | None | Unset = UNSET
    total_bytes_known: bool | Unset = False





    def to_dict(self) -> dict[str, Any]:
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        from ..models.run_switch_member_receipt import RunSwitchMemberReceipt # noqa: PLC0415
        completed_bytes = self.completed_bytes

        members: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.members, Unset):
            members = []
            for members_item_data in self.members:
                members_item = members_item_data.to_dict()
                members.append(members_item)



        operation: dict[str, Any] | None | Unset
        if isinstance(self.operation, Unset):
            operation = UNSET
        elif isinstance(self.operation, OperationProgress):
            operation = self.operation.to_dict()
        else:
            operation = self.operation

        phase: None | str | Unset
        if isinstance(self.phase, Unset):
            phase = UNSET
        elif isinstance(self.phase, str):
            phase = self.phase
        else:
            phase = self.phase

        total_bytes: int | None | Unset
        if isinstance(self.total_bytes, Unset):
            total_bytes = UNSET
        else:
            total_bytes = self.total_bytes

        total_bytes_known = self.total_bytes_known


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if completed_bytes is not UNSET:
            field_dict["completed_bytes"] = completed_bytes
        if members is not UNSET:
            field_dict["members"] = members
        if operation is not UNSET:
            field_dict["operation"] = operation
        if phase is not UNSET:
            field_dict["phase"] = phase
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes
        if total_bytes_known is not UNSET:
            field_dict["total_bytes_known"] = total_bytes_known

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        from ..models.run_switch_member_receipt import RunSwitchMemberReceipt # noqa: PLC0415
        d = dict(src_dict)
        completed_bytes = d.pop("completed_bytes", UNSET)

        _members = d.pop("members", UNSET)
        members: list[RunSwitchMemberReceipt] | Unset = UNSET
        if _members is not UNSET:
            members = []
            for members_item_data in _members:
                members_item = RunSwitchMemberReceipt.from_dict(members_item_data)



                members.append(members_item)


        def _parse_operation(data: object) -> None | OperationProgress | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                operation_type_0 = OperationProgress.from_dict(data)



                return operation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OperationProgress | Unset, data)

        operation = _parse_operation(d.pop("operation", UNSET))


        def _parse_phase(data: object) -> None | RunSwitchChildProgressPhaseType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                phase_type_0 = check_run_switch_child_progress_phase_type_0(data)



                return phase_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunSwitchChildProgressPhaseType0 | Unset, data)

        phase = _parse_phase(d.pop("phase", UNSET))


        def _parse_total_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        total_bytes_known = d.pop("total_bytes_known", UNSET)

        run_switch_child_progress = cls(
            completed_bytes=completed_bytes,
            members=members,
            operation=operation,
            phase=phase,
            total_bytes=total_bytes,
            total_bytes_known=total_bytes_known,
        )

        return run_switch_child_progress
