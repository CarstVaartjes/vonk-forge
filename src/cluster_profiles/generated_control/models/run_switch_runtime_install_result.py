from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="RunSwitchRuntimeInstallResult")



@_attrs_define
class RunSwitchRuntimeInstallResult:
    """
        Attributes:
            installation_id (str):
            phase (Literal['prepare']):
            subphase (Literal['runtime-install']):
     """

    installation_id: str
    phase: Literal['prepare']
    subphase: Literal['runtime-install']





    def to_dict(self) -> dict[str, Any]:
        installation_id = self.installation_id

        phase = self.phase

        subphase = self.subphase


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installation_id": installation_id,
            "phase": phase,
            "subphase": subphase,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        installation_id = d.pop("installation_id")

        phase = cast(Literal['prepare'] , d.pop("phase"))
        if phase != 'prepare':
            raise ValueError(f"phase must match const 'prepare', got '{phase}'")

        subphase = cast(Literal['runtime-install'] , d.pop("subphase"))
        if subphase != 'runtime-install':
            raise ValueError(f"subphase must match const 'runtime-install', got '{subphase}'")

        run_switch_runtime_install_result = cls(
            installation_id=installation_id,
            phase=phase,
            subphase=subphase,
        )

        return run_switch_runtime_install_result
