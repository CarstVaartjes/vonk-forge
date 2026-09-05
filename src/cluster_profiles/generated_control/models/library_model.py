from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.library_model_version_facts import LibraryModelVersionFacts
  from ..models.library_capability_inventory import LibraryCapabilityInventory
  from ..models.library_recipe_summary import LibraryRecipeSummary
  from ..models.model_version_identity import ModelVersionIdentity





T = TypeVar("T", bound="LibraryModel")



@_attrs_define
class LibraryModel:
    """
        Attributes:
            model (ModelVersionIdentity):
            recipes (list['LibraryRecipeSummary']):
            model_capabilities (Union[Unset, LibraryCapabilityInventory]): Compare-friendly model or recipe capability
                assertions with evidence state.
            model_version (Union['LibraryModelVersionFacts', None, Unset]):
            page_local (Union[Unset, bool]):  Default: True.
     """

    model: 'ModelVersionIdentity'
    recipes: list['LibraryRecipeSummary']
    model_capabilities: Union[Unset, 'LibraryCapabilityInventory'] = UNSET
    model_version: Union['LibraryModelVersionFacts', None, Unset] = UNSET
    page_local: Union[Unset, bool] = True





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_model_version_facts import LibraryModelVersionFacts
        from ..models.library_capability_inventory import LibraryCapabilityInventory
        from ..models.library_recipe_summary import LibraryRecipeSummary
        from ..models.model_version_identity import ModelVersionIdentity
        model = self.model.to_dict()

        recipes = []
        for recipes_item_data in self.recipes:
            recipes_item = recipes_item_data.to_dict()
            recipes.append(recipes_item)



        model_capabilities: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.model_capabilities, Unset):
            model_capabilities = self.model_capabilities.to_dict()

        model_version: Union[None, Unset, dict[str, Any]]
        if isinstance(self.model_version, Unset):
            model_version = UNSET
        elif isinstance(self.model_version, LibraryModelVersionFacts):
            model_version = self.model_version.to_dict()
        else:
            model_version = self.model_version

        page_local = self.page_local


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "model": model,
            "recipes": recipes,
        })
        if model_capabilities is not UNSET:
            field_dict["model_capabilities"] = model_capabilities
        if model_version is not UNSET:
            field_dict["model_version"] = model_version
        if page_local is not UNSET:
            field_dict["page_local"] = page_local

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_model_version_facts import LibraryModelVersionFacts
        from ..models.library_capability_inventory import LibraryCapabilityInventory
        from ..models.library_recipe_summary import LibraryRecipeSummary
        from ..models.model_version_identity import ModelVersionIdentity
        d = dict(src_dict)
        model = ModelVersionIdentity.from_dict(d.pop("model"))




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




        def _parse_model_version(data: object) -> Union['LibraryModelVersionFacts', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                model_version_type_0 = LibraryModelVersionFacts.from_dict(data)



                return model_version_type_0
            except: # noqa: E722
                pass
            return cast(Union['LibraryModelVersionFacts', None, Unset], data)

        model_version = _parse_model_version(d.pop("model_version", UNSET))


        page_local = d.pop("page_local", UNSET)

        library_model = cls(
            model=model,
            recipes=recipes,
            model_capabilities=model_capabilities,
            model_version=model_version,
            page_local=page_local,
        )

        return library_model
