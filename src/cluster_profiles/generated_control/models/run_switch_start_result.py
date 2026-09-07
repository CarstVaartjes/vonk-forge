from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_start_result_subphase_type_0 import check_run_switch_start_result_subphase_type_0
from ..models.run_switch_start_result_subphase_type_0 import RunSwitchStartResultSubphaseType0
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union






T = TypeVar("T", bound="RunSwitchStartResult")



@_attrs_define
class RunSwitchStartResult:
    """
        Attributes:
            phase (Literal['start']):
            run_id (str):
            subphase (Union[None, RunSwitchStartResultSubphaseType0, Unset]):
     """

    phase: Literal['start']
    run_id: str
    subphase: Union[None, RunSwitchStartResultSubphaseType0, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        phase = self.phase

        run_id = self.run_id

        subphase: Union[None, Unset, str]
        if isinstance(self.subphase, Unset):
            subphase = UNSET
        elif isinstance(self.subphase, str):
            subphase = self.subphase
        else:
            subphase = self.subphase


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "phase": phase,
            "run_id": run_id,
        })
        if subphase is not UNSET:
            field_dict["subphase"] = subphase

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        phase = cast(Literal['start'] , d.pop("phase"))
        if phase != 'start':
            raise ValueError(f"phase must match const 'start', got '{phase}'")

        run_id = d.pop("run_id")

        def _parse_subphase(data: object) -> Union[None, RunSwitchStartResultSubphaseType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                subphase_type_0 = check_run_switch_start_result_subphase_type_0(data)



                return subphase_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunSwitchStartResultSubphaseType0, Unset], data)

        subphase = _parse_subphase(d.pop("subphase", UNSET))


        run_switch_start_result = cls(
            phase=phase,
            run_id=run_id,
            subphase=subphase,
        )

        return run_switch_start_result
