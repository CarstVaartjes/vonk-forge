from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeUninstallResult")



@_attrs_define
class RecipeUninstallResult:
    """
        Attributes:
            removed_model_bytes (int):
            uninstalled (bool):
     """

    removed_model_bytes: int
    uninstalled: bool





    def to_dict(self) -> dict[str, Any]:
        removed_model_bytes = self.removed_model_bytes

        uninstalled = self.uninstalled


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "removed_model_bytes": removed_model_bytes,
            "uninstalled": uninstalled,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        removed_model_bytes = d.pop("removed_model_bytes")

        uninstalled = d.pop("uninstalled")

        recipe_uninstall_result = cls(
            removed_model_bytes=removed_model_bytes,
            uninstalled=uninstalled,
        )

        return recipe_uninstall_result
