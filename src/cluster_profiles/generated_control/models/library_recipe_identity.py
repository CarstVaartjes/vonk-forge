from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="LibraryRecipeIdentity")



@_attrs_define
class LibraryRecipeIdentity:
    """
        Attributes:
            content_sha256 (str):
            description (str):
            publisher (str):
            recipe_id (str):
            recipe_revision_id (str):
            slug (str):
            title (str):
     """

    content_sha256: str
    description: str
    publisher: str
    recipe_id: str
    recipe_revision_id: str
    slug: str
    title: str





    def to_dict(self) -> dict[str, Any]:
        content_sha256 = self.content_sha256

        description = self.description

        publisher = self.publisher

        recipe_id = self.recipe_id

        recipe_revision_id = self.recipe_revision_id

        slug = self.slug

        title = self.title


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "content_sha256": content_sha256,
            "description": description,
            "publisher": publisher,
            "recipe_id": recipe_id,
            "recipe_revision_id": recipe_revision_id,
            "slug": slug,
            "title": title,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        content_sha256 = d.pop("content_sha256")

        description = d.pop("description")

        publisher = d.pop("publisher")

        recipe_id = d.pop("recipe_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        slug = d.pop("slug")

        title = d.pop("title")

        library_recipe_identity = cls(
            content_sha256=content_sha256,
            description=description,
            publisher=publisher,
            recipe_id=recipe_id,
            recipe_revision_id=recipe_revision_id,
            slug=slug,
            title=title,
        )

        return library_recipe_identity
