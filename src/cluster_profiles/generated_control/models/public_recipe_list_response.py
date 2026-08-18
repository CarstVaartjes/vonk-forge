from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.public_recipe_list_item import PublicRecipeListItem





T = TypeVar("T", bound="PublicRecipeListResponse")



@_attrs_define
class PublicRecipeListResponse:
    """
        Attributes:
            commit (str):
            recipes (list['PublicRecipeListItem']):
            repository (str):
     """

    commit: str
    recipes: list['PublicRecipeListItem']
    repository: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.public_recipe_list_item import PublicRecipeListItem
        commit = self.commit

        recipes = []
        for recipes_item_data in self.recipes:
            recipes_item = recipes_item_data.to_dict()
            recipes.append(recipes_item)



        repository = self.repository


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "commit": commit,
            "recipes": recipes,
            "repository": repository,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.public_recipe_list_item import PublicRecipeListItem
        d = dict(src_dict)
        commit = d.pop("commit")

        recipes = []
        _recipes = d.pop("recipes")
        for recipes_item_data in (_recipes):
            recipes_item = PublicRecipeListItem.from_dict(recipes_item_data)



            recipes.append(recipes_item)


        repository = d.pop("repository")

        public_recipe_list_response = cls(
            commit=commit,
            recipes=recipes,
            repository=repository,
        )

        return public_recipe_list_response
