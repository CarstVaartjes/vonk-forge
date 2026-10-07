from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.bookkeeping_reason import BookkeepingReason
from ..models.bookkeeping_reason import check_bookkeeping_reason
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="Residue")



@_attrs_define
class Residue:
    """ A typed ``unknown``: what was damaged, why, and what the caller does next.

    It is a value, not an exception: the caller retires the damaged row or skips
    the element and carries on.

        Attributes:
            kind (str):
            reason (BookkeepingReason): Why bookkeeping was treated as unknown instead of refused.
            subject (str):
            note (str | Unset):  Default: ''.
     """

    kind: str
    reason: BookkeepingReason
    subject: str
    note: str | Unset = ''
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        kind = self.kind

        reason: str = self.reason

        subject = self.subject

        note = self.note


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "kind": kind,
            "reason": reason,
            "subject": subject,
        })
        if note is not UNSET:
            field_dict["note"] = note

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = d.pop("kind")

        reason = check_bookkeeping_reason(d.pop("reason"))




        subject = d.pop("subject")

        note = d.pop("note", UNSET)

        residue = cls(
            kind=kind,
            reason=reason,
            subject=subject,
            note=note,
        )


        residue.additional_properties = d
        return residue

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
