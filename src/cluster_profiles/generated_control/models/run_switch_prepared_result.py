from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="RunSwitchPreparedResult")



@_attrs_define
class RunSwitchPreparedResult:
    """
        Attributes:
            phase (Literal['prepare']):
            prepared (bool):
            subphase (Literal['runtime-plan']):
     """

    phase: Literal['prepare']
    prepared: bool
    subphase: Literal['runtime-plan']





    def to_dict(self) -> dict[str, Any]:
        phase = self.phase

        prepared = self.prepared

        subphase = self.subphase


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "phase": phase,
            "prepared": prepared,
            "subphase": subphase,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        phase = cast(Literal['prepare'] , d.pop("phase"))
        if phase != 'prepare':
            raise ValueError(f"phase must match const 'prepare', got '{phase}'")

        prepared = d.pop("prepared")

        subphase = cast(Literal['runtime-plan'] , d.pop("subphase"))
        if subphase != 'runtime-plan':
            raise ValueError(f"subphase must match const 'runtime-plan', got '{subphase}'")

        run_switch_prepared_result = cls(
            phase=phase,
            prepared=prepared,
            subphase=subphase,
        )

        return run_switch_prepared_result
