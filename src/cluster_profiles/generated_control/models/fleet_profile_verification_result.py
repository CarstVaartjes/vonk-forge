from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="FleetProfileVerificationResult")



@_attrs_define
class FleetProfileVerificationResult:
    """ Small result used by profile switch adapters that verify directly.

        Attributes:
            verified (bool):
     """

    verified: bool





    def to_dict(self) -> dict[str, Any]:
        verified = self.verified


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "verified": verified,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        verified = d.pop("verified")

        fleet_profile_verification_result = cls(
            verified=verified,
        )

        return fleet_profile_verification_result
