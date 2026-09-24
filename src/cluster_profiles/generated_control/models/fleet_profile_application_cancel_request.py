from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="FleetProfileApplicationCancelRequest")



@_attrs_define
class FleetProfileApplicationCancelRequest:
    """
        Attributes:
            profile_number (int):
            request_key (str):
     """

    profile_number: int
    request_key: str





    def to_dict(self) -> dict[str, Any]:
        profile_number = self.profile_number

        request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "profile_number": profile_number,
            "request_key": request_key,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        profile_number = d.pop("profile_number")

        request_key = d.pop("request_key")

        fleet_profile_application_cancel_request = cls(
            profile_number=profile_number,
            request_key=request_key,
        )

        return fleet_profile_application_cancel_request
