from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RemovedRecipeNodeResult")



@_attrs_define
class RemovedRecipeNodeResult:
    """ Result emitted by the Controller's logical uninstall projection.

        Attributes:
            removed (bool):
     """

    removed: bool





    def to_dict(self) -> dict[str, Any]:
        removed = self.removed


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "removed": removed,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        removed = d.pop("removed")

        removed_recipe_node_result = cls(
            removed=removed,
        )

        return removed_recipe_node_result
