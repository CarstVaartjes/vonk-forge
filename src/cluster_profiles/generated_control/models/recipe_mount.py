from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeMount")



@_attrs_define
class RecipeMount:
    """
        Attributes:
            read_only (bool):
            target (str):
     """

    read_only: bool
    target: str





    def to_dict(self) -> dict[str, Any]:
        read_only = self.read_only

        target = self.target


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "read_only": read_only,
            "target": target,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        read_only = d.pop("read_only")

        target = d.pop("target")

        recipe_mount = cls(
            read_only=read_only,
            target=target,
        )

        return recipe_mount
