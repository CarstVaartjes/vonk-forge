from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_cleanup_verify_result_subphase_type_0 import check_run_switch_cleanup_verify_result_subphase_type_0
from ..models.run_switch_cleanup_verify_result_subphase_type_0 import RunSwitchCleanupVerifyResultSubphaseType0
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union






T = TypeVar("T", bound="RunSwitchCleanupVerifyResult")



@_attrs_define
class RunSwitchCleanupVerifyResult:
    """ Observed removal of the installation, derived from durable state.

        Attributes:
            final_verified (bool):
            installation_id (str):
            phase (Literal['final_verify']):
            removed (bool):
            active_runs (Union[Unset, int]):  Default: 0.
            installation_state (Union[None, Unset, str]):
            subphase (Union[None, RunSwitchCleanupVerifyResultSubphaseType0, Unset]):
     """

    final_verified: bool
    installation_id: str
    phase: Literal['final_verify']
    removed: bool
    active_runs: Union[Unset, int] = 0
    installation_state: Union[None, Unset, str] = UNSET
    subphase: Union[None, RunSwitchCleanupVerifyResultSubphaseType0, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        final_verified = self.final_verified

        installation_id = self.installation_id

        phase = self.phase

        removed = self.removed

        active_runs = self.active_runs

        installation_state: Union[None, Unset, str]
        if isinstance(self.installation_state, Unset):
            installation_state = UNSET
        else:
            installation_state = self.installation_state

        subphase: Union[None, Unset, str]
        if isinstance(self.subphase, Unset):
            subphase = UNSET
        elif isinstance(self.subphase, str):
            subphase = self.subphase
        else:
            subphase = self.subphase


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "final_verified": final_verified,
            "installation_id": installation_id,
            "phase": phase,
            "removed": removed,
        })
        if active_runs is not UNSET:
            field_dict["active_runs"] = active_runs
        if installation_state is not UNSET:
            field_dict["installation_state"] = installation_state
        if subphase is not UNSET:
            field_dict["subphase"] = subphase

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        final_verified = d.pop("final_verified")

        installation_id = d.pop("installation_id")

        phase = cast(Literal['final_verify'] , d.pop("phase"))
        if phase != 'final_verify':
            raise ValueError(f"phase must match const 'final_verify', got '{phase}'")

        removed = d.pop("removed")

        active_runs = d.pop("active_runs", UNSET)

        def _parse_installation_state(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        installation_state = _parse_installation_state(d.pop("installation_state", UNSET))


        def _parse_subphase(data: object) -> Union[None, RunSwitchCleanupVerifyResultSubphaseType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                subphase_type_0 = check_run_switch_cleanup_verify_result_subphase_type_0(data)



                return subphase_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunSwitchCleanupVerifyResultSubphaseType0, Unset], data)

        subphase = _parse_subphase(d.pop("subphase", UNSET))


        run_switch_cleanup_verify_result = cls(
            final_verified=final_verified,
            installation_id=installation_id,
            phase=phase,
            removed=removed,
            active_runs=active_runs,
            installation_state=installation_state,
            subphase=subphase,
        )

        return run_switch_cleanup_verify_result
