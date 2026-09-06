from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RecipeIdentity")



@_attrs_define
class RecipeIdentity:
    """
        Attributes:
            publisher (str):
            slug (str):
     """

    publisher: str
    slug: str





    def to_dict(self) -> dict[str, Any]:
        publisher = self.publisher

        slug = self.slug


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "publisher": publisher,
            "slug": slug,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        publisher = d.pop("publisher")

        slug = d.pop("slug")

        recipe_identity = cls(
            publisher=publisher,
            slug=slug,
        )

        return recipe_identity
