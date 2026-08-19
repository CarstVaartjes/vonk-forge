from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="PublicRecipeListItem")



@_attrs_define
class PublicRecipeListItem:
    """
        Attributes:
            content_sha256 (str):
            description (str):
            publisher (str):
            slug (str):
            tags (list[str]):
            title (str):
            uri (str):
     """

    content_sha256: str
    description: str
    publisher: str
    slug: str
    tags: list[str]
    title: str
    uri: str





    def to_dict(self) -> dict[str, Any]:
        content_sha256 = self.content_sha256

        description = self.description

        publisher = self.publisher

        slug = self.slug

        tags = self.tags



        title = self.title

        uri = self.uri


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "content_sha256": content_sha256,
            "description": description,
            "publisher": publisher,
            "slug": slug,
            "tags": tags,
            "title": title,
            "uri": uri,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        content_sha256 = d.pop("content_sha256")

        description = d.pop("description")

        publisher = d.pop("publisher")

        slug = d.pop("slug")

        tags = cast(list[str], d.pop("tags"))


        title = d.pop("title")

        uri = d.pop("uri")

        public_recipe_list_item = cls(
            content_sha256=content_sha256,
            description=description,
            publisher=publisher,
            slug=slug,
            tags=tags,
            title=title,
            uri=uri,
        )

        return public_recipe_list_item
