from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Union

if TYPE_CHECKING:
  from ..models.library_recipe_summary import LibraryRecipeSummary
  from ..models.model_version_identity import ModelVersionIdentity





T = TypeVar("T", bound="LibraryModel")



@_attrs_define
class LibraryModel:
    """
        Attributes:
            model (ModelVersionIdentity):
            recipes (list['LibraryRecipeSummary']):
            page_local (Union[Unset, bool]):  Default: True.
     """

    model: 'ModelVersionIdentity'
    recipes: list['LibraryRecipeSummary']
    page_local: Union[Unset, bool] = True





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_recipe_summary import LibraryRecipeSummary
        from ..models.model_version_identity import ModelVersionIdentity
        model = self.model.to_dict()

        recipes = []
        for recipes_item_data in self.recipes:
            recipes_item = recipes_item_data.to_dict()
            recipes.append(recipes_item)



        page_local = self.page_local


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "model": model,
            "recipes": recipes,
        })
        if page_local is not UNSET:
            field_dict["page_local"] = page_local

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_recipe_summary import LibraryRecipeSummary
        from ..models.model_version_identity import ModelVersionIdentity
        d = dict(src_dict)
        model = ModelVersionIdentity.from_dict(d.pop("model"))




        recipes = []
        _recipes = d.pop("recipes")
        for recipes_item_data in (_recipes):
            recipes_item = LibraryRecipeSummary.from_dict(recipes_item_data)



            recipes.append(recipes_item)


        page_local = d.pop("page_local", UNSET)

        library_model = cls(
            model=model,
            recipes=recipes,
            page_local=page_local,
        )

        return library_model
