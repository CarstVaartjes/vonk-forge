from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, cast






T = TypeVar("T", bound="RecipeRemovalProjectionIssue")



@_attrs_define
class RecipeRemovalProjectionIssue:
    """
        Attributes:
            detail (str):
            next_action (str):
            code (Literal['recipe_image.removal_evidence_unavailable'] | Unset):  Default:
                'recipe_image.removal_evidence_unavailable'.
     """

    detail: str
    next_action: str
    code: Literal['recipe_image.removal_evidence_unavailable'] | Unset = 'recipe_image.removal_evidence_unavailable'
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        detail = self.detail

        next_action = self.next_action

        code = self.code


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
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

        code = cast(Literal['recipe_image.removal_evidence_unavailable'] | Unset , d.pop("code", UNSET))
        if code != 'recipe_image.removal_evidence_unavailable' and not isinstance(code, Unset):
            raise ValueError(f"code must match const 'recipe_image.removal_evidence_unavailable', got '{code}'")

        recipe_removal_projection_issue = cls(
            detail=detail,
            next_action=next_action,
            code=code,
        )


        recipe_removal_projection_issue.additional_properties = d
        return recipe_removal_projection_issue

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
