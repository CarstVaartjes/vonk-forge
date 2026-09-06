from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_progress_phase_type_0 import check_run_switch_progress_phase_type_0
from ..models.run_switch_progress_phase_type_0 import RunSwitchProgressPhaseType0
from ..models.run_switch_progress_state import check_run_switch_progress_state
from ..models.run_switch_progress_state import RunSwitchProgressState
from ..models.run_switch_progress_subphase_type_0 import check_run_switch_progress_subphase_type_0
from ..models.run_switch_progress_subphase_type_0 import RunSwitchProgressSubphaseType0
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.run_switch_member_progress import RunSwitchMemberProgress





T = TypeVar("T", bound="RunSwitchProgress")



@_attrs_define
class RunSwitchProgress:
    """
        Attributes:
            members (list['RunSwitchMemberProgress']):
            phase (Union[None, RunSwitchProgressPhaseType0]):
            phase_count (int):
            phase_index (int):
            state (RunSwitchProgressState):
            total_bytes_known (bool):
            completed_bytes (Union[Unset, int]):  Default: 0.
            subphase (Union[None, RunSwitchProgressSubphaseType0, Unset]):
            total_bytes (Union[None, Unset, int]):
     """

    members: list['RunSwitchMemberProgress']
    phase: Union[None, RunSwitchProgressPhaseType0]
    phase_count: int
    phase_index: int
    state: RunSwitchProgressState
    total_bytes_known: bool
    completed_bytes: Union[Unset, int] = 0
    subphase: Union[None, RunSwitchProgressSubphaseType0, Unset] = UNSET
    total_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_member_progress import RunSwitchMemberProgress
        members = []
        for members_item_data in self.members:
            members_item = members_item_data.to_dict()
            members.append(members_item)



        phase: Union[None, str]
        if isinstance(self.phase, str):
            phase = self.phase
        else:
            phase = self.phase

        phase_count = self.phase_count

        phase_index = self.phase_index

        state: str = self.state

        total_bytes_known = self.total_bytes_known

        completed_bytes = self.completed_bytes

        subphase: Union[None, Unset, str]
        if isinstance(self.subphase, Unset):
            subphase = UNSET
        elif isinstance(self.subphase, str):
            subphase = self.subphase
        else:
            subphase = self.subphase

        total_bytes: Union[None, Unset, int]
        if isinstance(self.total_bytes, Unset):
            total_bytes = UNSET
        else:
            total_bytes = self.total_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "members": members,
            "phase": phase,
            "phase_count": phase_count,
            "phase_index": phase_index,
            "state": state,
            "total_bytes_known": total_bytes_known,
        })
        if completed_bytes is not UNSET:
            field_dict["completed_bytes"] = completed_bytes
        if subphase is not UNSET:
            field_dict["subphase"] = subphase
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_member_progress import RunSwitchMemberProgress
        d = dict(src_dict)
        members = []
        _members = d.pop("members")
        for members_item_data in (_members):
            members_item = RunSwitchMemberProgress.from_dict(members_item_data)



            members.append(members_item)


        def _parse_phase(data: object) -> Union[None, RunSwitchProgressPhaseType0]:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                phase_type_0 = check_run_switch_progress_phase_type_0(data)



                return phase_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunSwitchProgressPhaseType0], data)

        phase = _parse_phase(d.pop("phase"))


        phase_count = d.pop("phase_count")

        phase_index = d.pop("phase_index")

        state = check_run_switch_progress_state(d.pop("state"))




        total_bytes_known = d.pop("total_bytes_known")

        completed_bytes = d.pop("completed_bytes", UNSET)

        def _parse_subphase(data: object) -> Union[None, RunSwitchProgressSubphaseType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                subphase_type_0 = check_run_switch_progress_subphase_type_0(data)



                return subphase_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunSwitchProgressSubphaseType0, Unset], data)

        subphase = _parse_subphase(d.pop("subphase", UNSET))


        def _parse_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        run_switch_progress = cls(
            members=members,
            phase=phase,
            phase_count=phase_count,
            phase_index=phase_index,
            state=state,
            total_bytes_known=total_bytes_known,
            completed_bytes=completed_bytes,
            subphase=subphase,
            total_bytes=total_bytes,
        )

        return run_switch_progress
