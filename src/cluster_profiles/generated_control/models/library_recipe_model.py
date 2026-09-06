from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_model_selection import RecipeModelSelection
  from ..models.model_definition import ModelDefinition





T = TypeVar("T", bound="LibraryRecipeModel")



@_attrs_define
class LibraryRecipeModel:
    """
        Attributes:
            model_document (ModelDefinition): One exact model version and variant, including its complete manifest.
            selection (RecipeModelSelection):
     """

    model_document: 'ModelDefinition'
    selection: 'RecipeModelSelection'





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_model_selection import RecipeModelSelection
        from ..models.model_definition import ModelDefinition
        model_document = self.model_document.to_dict()

        selection = self.selection.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "model_document": model_document,
            "selection": selection,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_model_selection import RecipeModelSelection
        from ..models.model_definition import ModelDefinition
        d = dict(src_dict)
        model_document = ModelDefinition.from_dict(d.pop("model_document"))




        selection = RecipeModelSelection.from_dict(d.pop("selection"))




        library_recipe_model = cls(
            model_document=model_document,
            selection=selection,
        )

        return library_recipe_model
