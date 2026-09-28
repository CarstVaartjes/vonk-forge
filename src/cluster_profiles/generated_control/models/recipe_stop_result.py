from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeStopResult")



@_attrs_define
class RecipeStopResult:
    """ A stop succeeds with an empty result.

     """






    def to_dict(self) -> dict[str, Any]:

        field_dict: dict[str, Any] = {}


        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        recipe_stop_result = cls(
        )

        return recipe_stop_result
