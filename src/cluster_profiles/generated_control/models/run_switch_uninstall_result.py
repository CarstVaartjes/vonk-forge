from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_uninstall_result_disposition import check_run_switch_uninstall_result_disposition
from ..models.run_switch_uninstall_result_disposition import RunSwitchUninstallResultDisposition
from ..models.run_switch_uninstall_result_subphase_type_0 import check_run_switch_uninstall_result_subphase_type_0
from ..models.run_switch_uninstall_result_subphase_type_0 import RunSwitchUninstallResultSubphaseType0
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="RunSwitchUninstallResult")



@_attrs_define
class RunSwitchUninstallResult:
    """ The removal of one installation that is no longer desired.

        Attributes:
            installation_id (str):
            phase (Literal['uninstall']):
            disposition (RunSwitchUninstallResultDisposition | Unset):  Default: 'uninstalled'.
            reason (None | str | Unset):
            subphase (None | RunSwitchUninstallResultSubphaseType0 | Unset):
     """

    installation_id: str
    phase: Literal['uninstall']
    disposition: RunSwitchUninstallResultDisposition | Unset = 'uninstalled'
    reason: None | str | Unset = UNSET
    subphase: None | RunSwitchUninstallResultSubphaseType0 | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        installation_id = self.installation_id

        phase = self.phase

        disposition: str | Unset = UNSET
        if not isinstance(self.disposition, Unset):
            disposition = self.disposition


        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        subphase: None | str | Unset
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
        disposition: RunSwitchUninstallResultDisposition | Unset
        if isinstance(_disposition,  Unset):
            disposition = UNSET
        else:
            disposition = check_run_switch_uninstall_result_disposition(_disposition)




        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))


        def _parse_subphase(data: object) -> None | RunSwitchUninstallResultSubphaseType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                subphase_type_0 = check_run_switch_uninstall_result_subphase_type_0(data)



                return subphase_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunSwitchUninstallResultSubphaseType0 | Unset, data)

        subphase = _parse_subphase(d.pop("subphase", UNSET))


        run_switch_uninstall_result = cls(
            installation_id=installation_id,
            phase=phase,
            disposition=disposition,
            reason=reason,
            subphase=subphase,
        )

        return run_switch_uninstall_result
