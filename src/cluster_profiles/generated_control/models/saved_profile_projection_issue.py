from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, cast






T = TypeVar("T", bound="SavedProfileProjectionIssue")



@_attrs_define
class SavedProfileProjectionIssue:
    """ An observation problem; it never authorizes changing saved intent.

        Attributes:
            detail (str):
            next_action (str):
            code (Literal['profile.definition_unavailable'] | Unset):  Default: 'profile.definition_unavailable'.
     """

    detail: str
    next_action: str
    code: Literal['profile.definition_unavailable'] | Unset = 'profile.definition_unavailable'





    def to_dict(self) -> dict[str, Any]:
        detail = self.detail

        next_action = self.next_action

        code = self.code


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "detail": detail,
            "next_action": next_action,
        })
        if code is not UNSET:
            field_dict["code"] = code

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        detail = d.pop("detail")

        next_action = d.pop("next_action")

        code = cast(Literal['profile.definition_unavailable'] | Unset , d.pop("code", UNSET))
        if code != 'profile.definition_unavailable' and not isinstance(code, Unset):
            raise ValueError(f"code must match const 'profile.definition_unavailable', got '{code}'")

        saved_profile_projection_issue = cls(
            detail=detail,
            next_action=next_action,
            code=code,
        )

        return saved_profile_projection_issue
