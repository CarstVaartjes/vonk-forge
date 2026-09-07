from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeOperationActivatedResult")



@_attrs_define
class RecipeOperationActivatedResult:
    """
        Attributes:
            activated (bool):
     """

    activated: bool





    def to_dict(self) -> dict[str, Any]:
        activated = self.activated


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "activated": activated,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        activated = d.pop("activated")

        recipe_operation_activated_result = cls(
            activated=activated,
        )

        return recipe_operation_activated_result
