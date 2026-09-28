from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="RecipeUpdateRequest")



@_attrs_define
class RecipeUpdateRequest:
    """
        Attributes:
            request_key (str):
            all_ (bool | Unset):  Default: False.
            selectors (list[str] | Unset):
     """

    request_key: str
    all_: bool | Unset = False
    selectors: list[str] | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        request_key = self.request_key

        all_ = self.all_

        selectors: list[str] | Unset = UNSET
        if not isinstance(self.selectors, Unset):
            selectors = self.selectors




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "request_key": request_key,
        })
        if all_ is not UNSET:
            field_dict["all"] = all_
        if selectors is not UNSET:
            field_dict["selectors"] = selectors

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        request_key = d.pop("request_key")

        all_ = d.pop("all", UNSET)

        selectors = cast(list[str], d.pop("selectors", UNSET))


        recipe_update_request = cls(
            request_key=request_key,
            all_=all_,
            selectors=selectors,
        )

        return recipe_update_request
