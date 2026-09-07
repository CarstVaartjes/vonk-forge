from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="FleetProfilePreparePreviewRequest")



@_attrs_define
class FleetProfilePreparePreviewRequest:
    """ Explicit empty body for the digest-bound preparation preview.

     """






    def to_dict(self) -> dict[str, Any]:

        field_dict: dict[str, Any] = {}


        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        fleet_profile_prepare_preview_request = cls(
        )

        return fleet_profile_prepare_preview_request
