from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_member_progress_phase_type_0 import check_run_switch_member_progress_phase_type_0
from ..models.run_switch_member_progress_phase_type_0 import RunSwitchMemberProgressPhaseType0
from ..models.run_switch_member_progress_state import check_run_switch_member_progress_state
from ..models.run_switch_member_progress_state import RunSwitchMemberProgressState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="RunSwitchMemberProgress")



@_attrs_define
class RunSwitchMemberProgress:
    """
        Attributes:
            node_id (str):
            state (RunSwitchMemberProgressState):
            completed_bytes (Union[Unset, int]):  Default: 0.
            error (Union[None, Unset, str]):
            phase (Union[None, RunSwitchMemberProgressPhaseType0, Unset]):
            total_bytes (Union[None, Unset, int]):
     """

    node_id: str
    state: RunSwitchMemberProgressState
    completed_bytes: Union[Unset, int] = 0
    error: Union[None, Unset, str] = UNSET
    phase: Union[None, RunSwitchMemberProgressPhaseType0, Unset] = UNSET
    total_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        state: str = self.state

        completed_bytes = self.completed_bytes

        error: Union[None, Unset, str]
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        phase: Union[None, Unset, str]
        if isinstance(self.phase, Unset):
            phase = UNSET
        elif isinstance(self.phase, str):
            phase = self.phase
        else:
            phase = self.phase

        total_bytes: Union[None, Unset, int]
        if isinstance(self.total_bytes, Unset):
            total_bytes = UNSET
        else:
            total_bytes = self.total_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "state": state,
        })
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

        state = check_run_switch_member_progress_state(d.pop("state"))




        completed_bytes = d.pop("completed_bytes", UNSET)

        def _parse_error(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        error = _parse_error(d.pop("error", UNSET))


        def _parse_phase(data: object) -> Union[None, RunSwitchMemberProgressPhaseType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                phase_type_0 = check_run_switch_member_progress_phase_type_0(data)



                return phase_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunSwitchMemberProgressPhaseType0, Unset], data)

        phase = _parse_phase(d.pop("phase", UNSET))


        def _parse_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        run_switch_member_progress = cls(
            node_id=node_id,
            state=state,
            completed_bytes=completed_bytes,
            error=error,
            phase=phase,
            total_bytes=total_bytes,
        )

        return run_switch_member_progress
