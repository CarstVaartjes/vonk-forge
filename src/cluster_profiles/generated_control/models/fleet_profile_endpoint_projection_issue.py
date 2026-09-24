from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="FleetProfileEndpointProjectionIssue")



@_attrs_define
class FleetProfileEndpointProjectionIssue:
    """ Safe diagnostic for immutable profile history that cannot be read.

        Attributes:
            code (Literal['profile.application_intent.invalid']):
            detail (str):
     """

    code: Literal['profile.application_intent.invalid']
    detail: str





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = cast(Literal['profile.application_intent.invalid'] , d.pop("code"))
        if code != 'profile.application_intent.invalid':
            raise ValueError(f"code must match const 'profile.application_intent.invalid', got '{code}'")

        detail = d.pop("detail")

        fleet_profile_endpoint_projection_issue = cls(
            code=code,
            detail=detail,
        )

        return fleet_profile_endpoint_projection_issue
