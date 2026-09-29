from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeImage")



@_attrs_define
class RecipeImage:
    """
        Attributes:
            digest (str):
            repository (str):
     """

    digest: str
    repository: str





    def to_dict(self) -> dict[str, Any]:
        digest = self.digest

        repository = self.repository


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "digest": digest,
            "repository": repository,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        digest = d.pop("digest")

        repository = d.pop("repository")

        recipe_image = cls(
            digest=digest,
            repository=repository,
        )

        return recipe_image
