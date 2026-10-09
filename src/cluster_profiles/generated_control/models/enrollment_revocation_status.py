from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="EnrollmentRevocationStatus")



@_attrs_define
class EnrollmentRevocationStatus:
    """
        Attributes:
            ca_confirmation_complete (bool):
            local_denial_complete (bool):
     """

    ca_confirmation_complete: bool
    local_denial_complete: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        ca_confirmation_complete = self.ca_confirmation_complete

        local_denial_complete = self.local_denial_complete


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "ca_confirmation_complete": ca_confirmation_complete,
            "local_denial_complete": local_denial_complete,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        ca_confirmation_complete = d.pop("ca_confirmation_complete")

        local_denial_complete = d.pop("local_denial_complete")

        enrollment_revocation_status = cls(
            ca_confirmation_complete=ca_confirmation_complete,
            local_denial_complete=local_denial_complete,
        )


        enrollment_revocation_status.additional_properties = d
        return enrollment_revocation_status

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
