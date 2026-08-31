from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ManagedCatalogStaleRecipe")



@_attrs_define
class ManagedCatalogStaleRecipe:
    """
        Attributes:
            current_revision_id (str):
            recipe_id (str):
            stale_installation_count (int):
            stale_run_count (int):
     """

    current_revision_id: str
    recipe_id: str
    stale_installation_count: int
    stale_run_count: int





    def to_dict(self) -> dict[str, Any]:
        current_revision_id = self.current_revision_id

        recipe_id = self.recipe_id

        stale_installation_count = self.stale_installation_count

        stale_run_count = self.stale_run_count


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "current_revision_id": current_revision_id,
            "recipe_id": recipe_id,
            "stale_installation_count": stale_installation_count,
            "stale_run_count": stale_run_count,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        current_revision_id = d.pop("current_revision_id")

        recipe_id = d.pop("recipe_id")

        stale_installation_count = d.pop("stale_installation_count")

        stale_run_count = d.pop("stale_run_count")

        managed_catalog_stale_recipe = cls(
            current_revision_id=current_revision_id,
            recipe_id=recipe_id,
            stale_installation_count=stale_installation_count,
            stale_run_count=stale_run_count,
        )

        return managed_catalog_stale_recipe
