from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_cleanup_result_subphase_type_0 import check_run_switch_cleanup_result_subphase_type_0
from ..models.run_switch_cleanup_result_subphase_type_0 import RunSwitchCleanupResultSubphaseType0
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union






T = TypeVar("T", bound="RunSwitchCleanupResult")



@_attrs_define
class RunSwitchCleanupResult:
    """
        Attributes:
            nas_evicted (bool):
            phase (Literal['cleanup']):
            reclaimed_bytes (int):
            scope (Literal['spark-local']):
            protected_digests (Union[Unset, list[str]]):
            protected_referenced_bytes (Union[Unset, int]):  Default: 0.
            reclaimed_digests (Union[Unset, list[str]]):
            subphase (Union[None, RunSwitchCleanupResultSubphaseType0, Unset]):
     """

    nas_evicted: bool
    phase: Literal['cleanup']
    reclaimed_bytes: int
    scope: Literal['spark-local']
    protected_digests: Union[Unset, list[str]] = UNSET
    protected_referenced_bytes: Union[Unset, int] = 0
    reclaimed_digests: Union[Unset, list[str]] = UNSET
    subphase: Union[None, RunSwitchCleanupResultSubphaseType0, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        nas_evicted = self.nas_evicted

        phase = self.phase

        reclaimed_bytes = self.reclaimed_bytes

        scope = self.scope

        protected_digests: Union[Unset, list[str]] = UNSET
        if not isinstance(self.protected_digests, Unset):
            protected_digests = self.protected_digests



        protected_referenced_bytes = self.protected_referenced_bytes

        reclaimed_digests: Union[Unset, list[str]] = UNSET
        if not isinstance(self.reclaimed_digests, Unset):
            reclaimed_digests = self.reclaimed_digests



        subphase: Union[None, Unset, str]
        if isinstance(self.subphase, Unset):
            subphase = UNSET
        elif isinstance(self.subphase, str):
            subphase = self.subphase
        else:
            subphase = self.subphase


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "nas_evicted": nas_evicted,
            "phase": phase,
            "reclaimed_bytes": reclaimed_bytes,
            "scope": scope,
        })
        if protected_digests is not UNSET:
            field_dict["protected_digests"] = protected_digests
        if protected_referenced_bytes is not UNSET:
            field_dict["protected_referenced_bytes"] = protected_referenced_bytes
        if reclaimed_digests is not UNSET:
            field_dict["reclaimed_digests"] = reclaimed_digests
        if subphase is not UNSET:
            field_dict["subphase"] = subphase

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        nas_evicted = d.pop("nas_evicted")

        phase = cast(Literal['cleanup'] , d.pop("phase"))
        if phase != 'cleanup':
            raise ValueError(f"phase must match const 'cleanup', got '{phase}'")

        reclaimed_bytes = d.pop("reclaimed_bytes")

        scope = cast(Literal['spark-local'] , d.pop("scope"))
        if scope != 'spark-local':
            raise ValueError(f"scope must match const 'spark-local', got '{scope}'")

        protected_digests = cast(list[str], d.pop("protected_digests", UNSET))


        protected_referenced_bytes = d.pop("protected_referenced_bytes", UNSET)

        reclaimed_digests = cast(list[str], d.pop("reclaimed_digests", UNSET))


        def _parse_subphase(data: object) -> Union[None, RunSwitchCleanupResultSubphaseType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                subphase_type_0 = check_run_switch_cleanup_result_subphase_type_0(data)



                return subphase_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RunSwitchCleanupResultSubphaseType0, Unset], data)

        subphase = _parse_subphase(d.pop("subphase", UNSET))


        run_switch_cleanup_result = cls(
            nas_evicted=nas_evicted,
            phase=phase,
            reclaimed_bytes=reclaimed_bytes,
            scope=scope,
            protected_digests=protected_digests,
            protected_referenced_bytes=protected_referenced_bytes,
            reclaimed_digests=reclaimed_digests,
            subphase=subphase,
        )

        return run_switch_cleanup_result
