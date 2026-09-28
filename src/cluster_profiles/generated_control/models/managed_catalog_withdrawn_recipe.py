from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="ManagedCatalogWithdrawnRecipe")



@_attrs_define
class ManagedCatalogWithdrawnRecipe:
    """
        Attributes:
            recipe_id (str):
            recipe_uri (None | str | Unset):
            release_version (None | str | Unset):
     """

    recipe_id: str
    recipe_uri: None | str | Unset = UNSET
    release_version: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        recipe_id = self.recipe_id

        recipe_uri: None | str | Unset
        if isinstance(self.recipe_uri, Unset):
            recipe_uri = UNSET
        else:
            recipe_uri = self.recipe_uri

        release_version: None | str | Unset
        if isinstance(self.release_version, Unset):
            release_version = UNSET
        else:
            release_version = self.release_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "recipe_id": recipe_id,
        })
        if recipe_uri is not UNSET:
            field_dict["recipe_uri"] = recipe_uri
        if release_version is not UNSET:
            field_dict["release_version"] = release_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        recipe_id = d.pop("recipe_id")

        def _parse_recipe_uri(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        recipe_uri = _parse_recipe_uri(d.pop("recipe_uri", UNSET))


        def _parse_release_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        release_version = _parse_release_version(d.pop("release_version", UNSET))


        managed_catalog_withdrawn_recipe = cls(
            recipe_id=recipe_id,
            recipe_uri=recipe_uri,
            release_version=release_version,
        )

        return managed_catalog_withdrawn_recipe
