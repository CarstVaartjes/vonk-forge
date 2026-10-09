from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RenewalWindow")



@_attrs_define
class RenewalWindow:
    """ Offsets from issuance, in seconds; a client picks once per credential.

        Attributes:
            end_seconds (int):
            start_seconds (int):
     """

    end_seconds: int
    start_seconds: int





    def to_dict(self) -> dict[str, Any]:
        end_seconds = self.end_seconds

        start_seconds = self.start_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "end_seconds": end_seconds,
            "start_seconds": start_seconds,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        end_seconds = d.pop("end_seconds")

        start_seconds = d.pop("start_seconds")

        renewal_window = cls(
            end_seconds=end_seconds,
            start_seconds=start_seconds,
        )

        return renewal_window
