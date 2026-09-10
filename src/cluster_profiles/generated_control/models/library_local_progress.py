from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_local_progress_state import check_library_local_progress_state
from ..models.library_local_progress_state import LibraryLocalProgressState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="LibraryLocalProgress")



@_attrs_define
class LibraryLocalProgress:
    """ Observable progress for a Controller-local preparation operation.

        Attributes:
            state (LibraryLocalProgressState):
            completed_bytes (Union[Unset, int]):  Default: 0.
            operation_id (Union[None, Unset, str]):
            phase (Union[None, Unset, str]):
            total_bytes (Union[None, Unset, int]):
     """

    state: LibraryLocalProgressState
    completed_bytes: Union[Unset, int] = 0
    operation_id: Union[None, Unset, str] = UNSET
    phase: Union[None, Unset, str] = UNSET
    total_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        state: str = self.state

        completed_bytes = self.completed_bytes

        operation_id: Union[None, Unset, str]
        if isinstance(self.operation_id, Unset):
            operation_id = UNSET
        else:
            operation_id = self.operation_id

        phase: Union[None, Unset, str]
        if isinstance(self.phase, Unset):
            phase = UNSET
        else:
            phase = self.phase

        total_bytes: Union[None, Unset, int]
        if isinstance(self.total_bytes, Unset):
            total_bytes = UNSET
        else:
            total_bytes = self.total_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "state": state,
        })
        if completed_bytes is not UNSET:
            field_dict["completed_bytes"] = completed_bytes
        if operation_id is not UNSET:
            field_dict["operation_id"] = operation_id
        if phase is not UNSET:
            field_dict["phase"] = phase
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        state = check_library_local_progress_state(d.pop("state"))




        completed_bytes = d.pop("completed_bytes", UNSET)

        def _parse_operation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        operation_id = _parse_operation_id(d.pop("operation_id", UNSET))


        def _parse_phase(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        phase = _parse_phase(d.pop("phase", UNSET))


        def _parse_total_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        library_local_progress = cls(
            state=state,
            completed_bytes=completed_bytes,
            operation_id=operation_id,
            phase=phase,
            total_bytes=total_bytes,
        )

        return library_local_progress
