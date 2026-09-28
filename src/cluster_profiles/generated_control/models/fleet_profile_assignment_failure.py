from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="FleetProfileAssignmentFailure")



@_attrs_define
class FleetProfileAssignmentFailure:
    """
        Attributes:
            reason (str):
            assignment_id (Union[None, Unset, str]):
            operation_id (Union[None, Unset, str]):
            terminal (Union[Unset, bool]):  Default: False.
     """

    reason: str
    assignment_id: Union[None, Unset, str] = UNSET
    operation_id: Union[None, Unset, str] = UNSET
    terminal: Union[Unset, bool] = False





    def to_dict(self) -> dict[str, Any]:
        reason = self.reason

        assignment_id: Union[None, Unset, str]
        if isinstance(self.assignment_id, Unset):
            assignment_id = UNSET
        else:
            assignment_id = self.assignment_id

        operation_id: Union[None, Unset, str]
        if isinstance(self.operation_id, Unset):
            operation_id = UNSET
        else:
            operation_id = self.operation_id

        terminal = self.terminal


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "reason": reason,
        })
        if assignment_id is not UNSET:
            field_dict["assignment_id"] = assignment_id
        if operation_id is not UNSET:
            field_dict["operation_id"] = operation_id
        if terminal is not UNSET:
            field_dict["terminal"] = terminal

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        reason = d.pop("reason")

        def _parse_assignment_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        assignment_id = _parse_assignment_id(d.pop("assignment_id", UNSET))


        def _parse_operation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        operation_id = _parse_operation_id(d.pop("operation_id", UNSET))


        terminal = d.pop("terminal", UNSET)

        fleet_profile_assignment_failure = cls(
            reason=reason,
            assignment_id=assignment_id,
            operation_id=operation_id,
            terminal=terminal,
        )

        return fleet_profile_assignment_failure
