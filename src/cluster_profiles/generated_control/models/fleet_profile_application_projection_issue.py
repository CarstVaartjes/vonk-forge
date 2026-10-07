from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, cast






T = TypeVar("T", bound="FleetProfileApplicationProjectionIssue")



@_attrs_define
class FleetProfileApplicationProjectionIssue:
    """ Historical state is retained while its metadata cannot be verified.

        Attributes:
            code (Literal['profile.application_intent.invalid']):
            detail (str):
            observation (Literal['unknown'] | Unset):  Default: 'unknown'.
     """

    code: Literal['profile.application_intent.invalid']
    detail: str
    observation: Literal['unknown'] | Unset = 'unknown'





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail

        observation = self.observation


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
        })
        if observation is not UNSET:
            field_dict["observation"] = observation

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = cast(Literal['profile.application_intent.invalid'] , d.pop("code"))
        if code != 'profile.application_intent.invalid':
            raise ValueError(f"code must match const 'profile.application_intent.invalid', got '{code}'")

        detail = d.pop("detail")

        observation = cast(Literal['unknown'] | Unset , d.pop("observation", UNSET))
        if observation != 'unknown' and not isinstance(observation, Unset):
            raise ValueError(f"observation must match const 'unknown', got '{observation}'")

        fleet_profile_application_projection_issue = cls(
            code=code,
            detail=detail,
            observation=observation,
        )

        return fleet_profile_application_projection_issue
