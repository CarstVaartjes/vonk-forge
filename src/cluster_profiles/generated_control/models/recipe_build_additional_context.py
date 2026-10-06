from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeBuildAdditionalContext")



@_attrs_define
class RecipeBuildAdditionalContext:
    """
        Attributes:
            name (str):
            path (str):
     """

    name: str
    path: str





    def to_dict(self) -> dict[str, Any]:
        name = self.name

        path = self.path


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "name": name,
            "path": path,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        path = d.pop("path")

        recipe_build_additional_context = cls(
            name=name,
            path=path,
        )

        return recipe_build_additional_context
