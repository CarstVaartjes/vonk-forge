from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_member_receipt_phase_type_0 import check_run_switch_member_receipt_phase_type_0
from ..models.run_switch_member_receipt_phase_type_0 import RunSwitchMemberReceiptPhaseType0
from ..models.run_switch_member_receipt_state import check_run_switch_member_receipt_state
from ..models.run_switch_member_receipt_state import RunSwitchMemberReceiptState
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="RunSwitchMemberReceipt")



@_attrs_define
class RunSwitchMemberReceipt:
    """ Durable member projection emitted by a child distribution operation.

        Attributes:
            node_id (str):
            state (RunSwitchMemberReceiptState):
            cached (bool | Unset):  Default: False.
            completed_bytes (int | Unset):  Default: 0.
            error (None | str | Unset):
            phase (None | RunSwitchMemberReceiptPhaseType0 | Unset):
            total_bytes (int | None | Unset):
     """

    node_id: str
    state: RunSwitchMemberReceiptState
    cached: bool | Unset = False
    completed_bytes: int | Unset = 0
    error: None | str | Unset = UNSET
    phase: None | RunSwitchMemberReceiptPhaseType0 | Unset = UNSET
    total_bytes: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        state: str = self.state

        cached = self.cached

        completed_bytes = self.completed_bytes

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

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


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "state": state,
        })
        if cached is not UNSET:
            field_dict["cached"] = cached
        if completed_bytes is not UNSET:
            field_dict["completed_bytes"] = completed_bytes
        if error is not UNSET:
            field_dict["error"] = error
        if phase is not UNSET:
            field_dict["phase"] = phase
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = d.pop("node_id")

        state = check_run_switch_member_receipt_state(d.pop("state"))




        cached = d.pop("cached", UNSET)

        completed_bytes = d.pop("completed_bytes", UNSET)

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))


        def _parse_phase(data: object) -> None | RunSwitchMemberReceiptPhaseType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                phase_type_0 = check_run_switch_member_receipt_phase_type_0(data)



                return phase_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunSwitchMemberReceiptPhaseType0 | Unset, data)

        phase = _parse_phase(d.pop("phase", UNSET))


        def _parse_total_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        run_switch_member_receipt = cls(
            node_id=node_id,
            state=state,
            cached=cached,
            completed_bytes=completed_bytes,
            error=error,
            phase=phase,
            total_bytes=total_bytes,
        )

        return run_switch_member_receipt
