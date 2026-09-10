from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Union






T = TypeVar("T", bound="FleetEnrollRequest")



@_attrs_define
class FleetEnrollRequest:
    """
        Attributes:
            name (str):
            ttl_seconds (Union[Unset, int]):  Default: 900.
     """

    name: str
    ttl_seconds: Union[Unset, int] = 900





    def to_dict(self) -> dict[str, Any]:
        name = self.name

        ttl_seconds = self.ttl_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "name": name,
        })
        if ttl_seconds is not UNSET:
            field_dict["ttl_seconds"] = ttl_seconds

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        ttl_seconds = d.pop("ttl_seconds", UNSET)

        fleet_enroll_request = cls(
            name=name,
            ttl_seconds=ttl_seconds,
        )

        return fleet_enroll_request
