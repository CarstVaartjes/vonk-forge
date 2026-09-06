from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Union

if TYPE_CHECKING:
  from ..models.library_model_identity import LibraryModelIdentity
  from ..models.library_capability_inventory import LibraryCapabilityInventory
  from ..models.library_recipe_summary import LibraryRecipeSummary
  from ..models.model_definition import ModelDefinition





T = TypeVar("T", bound="LibraryModel")



@_attrs_define
class LibraryModel:
    """
        Attributes:
            model (LibraryModelIdentity): Content-addressed identity for a canonical Model document.
            model_document (ModelDefinition): One exact model version and variant, including its complete manifest.
            recipes (list['LibraryRecipeSummary']):
            model_capabilities (Union[Unset, LibraryCapabilityInventory]): Compare-friendly model or recipe capability
                assertions with evidence state.
            page_local (Union[Unset, bool]):  Default: True.
     """

    model: 'LibraryModelIdentity'
    model_document: 'ModelDefinition'
    recipes: list['LibraryRecipeSummary']
    model_capabilities: Union[Unset, 'LibraryCapabilityInventory'] = UNSET
    page_local: Union[Unset, bool] = True





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_model_identity import LibraryModelIdentity
        from ..models.library_capability_inventory import LibraryCapabilityInventory
        from ..models.library_recipe_summary import LibraryRecipeSummary
        from ..models.model_definition import ModelDefinition
        model = self.model.to_dict()

        model_document = self.model_document.to_dict()

        recipes = []
        for recipes_item_data in self.recipes:
            recipes_item = recipes_item_data.to_dict()
            recipes.append(recipes_item)



        model_capabilities: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.model_capabilities, Unset):
            model_capabilities = self.model_capabilities.to_dict()

        page_local = self.page_local


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "model": model,
            "model_document": model_document,
            "recipes": recipes,
        })
        if model_capabilities is not UNSET:
            field_dict["model_capabilities"] = model_capabilities
        if page_local is not UNSET:
            field_dict["page_local"] = page_local

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_model_identity import LibraryModelIdentity
        from ..models.library_capability_inventory import LibraryCapabilityInventory
        from ..models.library_recipe_summary import LibraryRecipeSummary
        from ..models.model_definition import ModelDefinition
        d = dict(src_dict)
        model = LibraryModelIdentity.from_dict(d.pop("model"))




        model_document = ModelDefinition.from_dict(d.pop("model_document"))




        recipes = []
        _recipes = d.pop("recipes")
        for recipes_item_data in (_recipes):
            recipes_item = LibraryRecipeSummary.from_dict(recipes_item_data)



            recipes.append(recipes_item)


        _model_capabilities = d.pop("model_capabilities", UNSET)
        model_capabilities: Union[Unset, LibraryCapabilityInventory]
        if isinstance(_model_capabilities,  Unset):
            model_capabilities = UNSET
        else:
            model_capabilities = LibraryCapabilityInventory.from_dict(_model_capabilities)




        page_local = d.pop("page_local", UNSET)

        library_model = cls(
            model=model,
            model_document=model_document,
            recipes=recipes,
            model_capabilities=model_capabilities,
            page_local=page_local,
        )

        return library_model
