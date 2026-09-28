from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="FleetEnrollRequest")



@_attrs_define
class FleetEnrollRequest:
    """
        Attributes:
            name (str):
            request_key (str):
     """

    name: str
    request_key: str





    def to_dict(self) -> dict[str, Any]:
        name = self.name

        request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "name": name,
            "request_key": request_key,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        request_key = d.pop("request_key")

        fleet_enroll_request = cls(
            name=name,
            request_key=request_key,
        )

        return fleet_enroll_request
