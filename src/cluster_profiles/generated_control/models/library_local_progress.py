from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_local_progress_state import check_library_local_progress_state
from ..models.library_local_progress_state import LibraryLocalProgressState
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="LibraryLocalProgress")



@_attrs_define
class LibraryLocalProgress:
    """ Observable progress for a Controller-local preparation operation.

        Attributes:
            state (LibraryLocalProgressState):
            completed_bytes (int | Unset):  Default: 0.
            operation_id (None | str | Unset):
            phase (None | str | Unset):
            total_bytes (int | None | Unset):
     """

    state: LibraryLocalProgressState
    completed_bytes: int | Unset = 0
    operation_id: None | str | Unset = UNSET
    phase: None | str | Unset = UNSET
    total_bytes: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        state: str = self.state

        completed_bytes = self.completed_bytes

        operation_id: None | str | Unset
        if isinstance(self.operation_id, Unset):
            operation_id = UNSET
        else:
            operation_id = self.operation_id

        phase: None | str | Unset
        if isinstance(self.phase, Unset):
            phase = UNSET
        else:
            phase = self.phase

        total_bytes: int | None | Unset
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

        def _parse_operation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        operation_id = _parse_operation_id(d.pop("operation_id", UNSET))


        def _parse_phase(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        phase = _parse_phase(d.pop("phase", UNSET))


        def _parse_total_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        total_bytes = _parse_total_bytes(d.pop("total_bytes", UNSET))


        library_local_progress = cls(
            state=state,
            completed_bytes=completed_bytes,
            operation_id=operation_id,
            phase=phase,
            total_bytes=total_bytes,
        )

        return library_local_progress
