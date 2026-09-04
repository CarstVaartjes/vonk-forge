from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="FleetProfilePrepareRequest")



@_attrs_define
class FleetProfilePrepareRequest:
    """
        Attributes:
            plan_digest (str):
            request_key (str):
     """

    plan_digest: str
    request_key: str





    def to_dict(self) -> dict[str, Any]:
        plan_digest = self.plan_digest

        request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "plan_digest": plan_digest,
            "request_key": request_key,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        plan_digest = d.pop("plan_digest")

        request_key = d.pop("request_key")

        fleet_profile_prepare_request = cls(
            plan_digest=plan_digest,
            request_key=request_key,
        )

        return fleet_profile_prepare_request
