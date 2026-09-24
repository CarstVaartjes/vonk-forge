from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Union






T = TypeVar("T", bound="RecipeUpdateScope")



@_attrs_define
class RecipeUpdateScope:
    """
        Attributes:
            all_ (Union[Unset, bool]):  Default: False.
            selectors (Union[Unset, list[str]]):
     """

    all_: Union[Unset, bool] = False
    selectors: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        all_ = self.all_

        selectors: Union[Unset, list[str]] = UNSET
        if not isinstance(self.selectors, Unset):
            selectors = self.selectors




        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if all_ is not UNSET:
            field_dict["all"] = all_
        if selectors is not UNSET:
            field_dict["selectors"] = selectors

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        all_ = d.pop("all", UNSET)

        selectors = cast(list[str], d.pop("selectors", UNSET))


        recipe_update_scope = cls(
            all_=all_,
            selectors=selectors,
        )

        return recipe_update_scope
