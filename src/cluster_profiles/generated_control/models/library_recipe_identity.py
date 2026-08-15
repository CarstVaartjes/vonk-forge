from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_recipe_identity_source_kind import check_library_recipe_identity_source_kind
from ..models.library_recipe_identity_source_kind import LibraryRecipeIdentitySourceKind
from typing import cast






T = TypeVar("T", bound="LibraryRecipeIdentity")



@_attrs_define
class LibraryRecipeIdentity:
    """
        Attributes:
            description (str):
            recipe_id (str):
            slug (str):
            source_kind (LibraryRecipeIdentitySourceKind):
            title (str):
     """

    description: str
    recipe_id: str
    slug: str
    source_kind: LibraryRecipeIdentitySourceKind
    title: str





    def to_dict(self) -> dict[str, Any]:
        description = self.description

        recipe_id = self.recipe_id

        slug = self.slug

        source_kind: str = self.source_kind

        title = self.title


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "description": description,
            "recipe_id": recipe_id,
            "slug": slug,
            "source_kind": source_kind,
            "title": title,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        description = d.pop("description")

        recipe_id = d.pop("recipe_id")

        slug = d.pop("slug")

        source_kind = check_library_recipe_identity_source_kind(d.pop("source_kind"))




        title = d.pop("title")

        library_recipe_identity = cls(
            description=description,
            recipe_id=recipe_id,
            slug=slug,
            source_kind=source_kind,
            title=title,
        )

        return library_recipe_identity
