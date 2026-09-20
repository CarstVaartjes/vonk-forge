from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_uninstall_result_disposition import check_run_switch_uninstall_result_disposition
from ..models.run_switch_uninstall_result_disposition import RunSwitchUninstallResultDisposition
from ..models.run_switch_uninstall_result_subphase_type_0 import check_run_switch_uninstall_result_subphase_type_0
from ..models.run_switch_uninstall_result_subphase_type_0 import RunSwitchUninstallResultSubphaseType0
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union






T = TypeVar("T", bound="RunSwitchUninstallResult")



@_attrs_define
class RunSwitchUninstallResult:
    """ The removal of one installation that is no longer desired.

        Attributes:
            installation_id (str):
            phase (Literal['uninstall']):
            disposition (Union[Unset, RunSwitchUninstallResultDisposition]):  Default: 'uninstalled'.
            reason (Union[None, Unset, str]):
            subphase (Union[None, RunSwitchUninstallResultSubphaseType0, Unset]):
     """

    installation_id: str
    phase: Literal['uninstall']
    disposition: Union[Unset, RunSwitchUninstallResultDisposition] = 'uninstalled'
    reason: Union[None, Unset, str] = UNSET
    subphase: Union[None, RunSwitchUninstallResultSubphaseType0, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        installation_id = self.installation_id

        phase = self.phase

        disposition: Union[Unset, str] = UNSET
        if not isinstance(self.disposition, Unset):
            disposition = self.disposition


        reason: Union[None, Unset, str]
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        subphase: Union[None, Unset, str]
        if isinstance(self.subphase, Unset):
            subphase = UNSET
        elif isinstance(self.subphase, str):
            subphase = self.subphase
        else:
            subphase = self.subphase


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installation_id": installation_id,
            "phase": phase,
        })
        if disposition is not UNSET:
            field_dict["disposition"] = disposition
        if reason is not UNSET:
            field_dict["reason"] = reason
        if subphase is not UNSET:
            field_dict["subphase"] = subphase

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        installation_id = d.pop("installation_id")

        phase = cast(Literal['uninstall'] , d.pop("phase"))
        if phase != 'uninstall':
            raise ValueError(f"phase must match const 'uninstall', got '{phase}'")

        _disposition = d.pop("disposition", UNSET)
        disposition: Union[Unset, RunSwitchUninstallResultDisposition]
        if isinstance(_disposition,  Unset):
            disposition = UNSET
        else:
            disposition = check_run_switch_uninstall_result_disposition(_disposition)




        def _parse_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        reason = _parse_reason(d.pop("reason", UNSET))


        def _parse_subphase(data: object) -> Union[None, RunSwitchUninstallResultSubphaseType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                subphase_type_0 = check_run_switch_uninstall_result_subphase_type_0(data)



                return subphase_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunSwitchUninstallResultSubphaseType0, Unset], data)

        subphase = _parse_subphase(d.pop("subphase", UNSET))


        run_switch_uninstall_result = cls(
            installation_id=installation_id,
            phase=phase,
            disposition=disposition,
            reason=reason,
            subphase=subphase,
        )

        return run_switch_uninstall_result
