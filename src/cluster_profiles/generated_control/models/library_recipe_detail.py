from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.operational_state import OperationalState
  from ..models.library_recipe_identity import LibraryRecipeIdentity
  from ..models.library_recipe_model import LibraryRecipeModel
  from ..models.library_capability_inventory import LibraryCapabilityInventory
  from ..models.recipe_topology import RecipeTopology
  from ..models.topology_placement import TopologyPlacement
  from ..models.recipe_definition import RecipeDefinition
  from ..models.library_projection_reason import LibraryProjectionReason





T = TypeVar("T", bound="LibraryRecipeDetail")



@_attrs_define
class LibraryRecipeDetail:
    """
        Attributes:
            definition (RecipeDefinition): The sole public recipe authoring contract.
            generated_at (datetime.datetime):
            model_documents (list['LibraryRecipeModel']):
            operational_state (OperationalState):
            placement (list['TopologyPlacement']):
            reasons (list['LibraryProjectionReason']):
            recipe (LibraryRecipeIdentity):
            topology (Union['RecipeTopology', None]):
            model_capabilities (Union[Unset, LibraryCapabilityInventory]): Compare-friendly model or recipe capability
                assertions with evidence state.
            recipe_capabilities (Union[Unset, LibraryCapabilityInventory]): Compare-friendly model or recipe capability
                assertions with evidence state.
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    definition: 'RecipeDefinition'
    generated_at: datetime.datetime
    model_documents: list['LibraryRecipeModel']
    operational_state: 'OperationalState'
    placement: list['TopologyPlacement']
    reasons: list['LibraryProjectionReason']
    recipe: 'LibraryRecipeIdentity'
    topology: Union['RecipeTopology', None]
    model_capabilities: Union[Unset, 'LibraryCapabilityInventory'] = UNSET
    recipe_capabilities: Union[Unset, 'LibraryCapabilityInventory'] = UNSET
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.operational_state import OperationalState
        from ..models.library_recipe_identity import LibraryRecipeIdentity
        from ..models.library_recipe_model import LibraryRecipeModel
        from ..models.library_capability_inventory import LibraryCapabilityInventory
        from ..models.recipe_topology import RecipeTopology
        from ..models.topology_placement import TopologyPlacement
        from ..models.recipe_definition import RecipeDefinition
        from ..models.library_projection_reason import LibraryProjectionReason
        definition = self.definition.to_dict()

        generated_at = self.generated_at.isoformat()

        model_documents = []
        for model_documents_item_data in self.model_documents:
            model_documents_item = model_documents_item_data.to_dict()
            model_documents.append(model_documents_item)



        operational_state = self.operational_state.to_dict()

        placement = []
        for placement_item_data in self.placement:
            placement_item = placement_item_data.to_dict()
            placement.append(placement_item)



        reasons = []
        for reasons_item_data in self.reasons:
            reasons_item = reasons_item_data.to_dict()
            reasons.append(reasons_item)



        recipe = self.recipe.to_dict()

        topology: Union[None, dict[str, Any]]
        if isinstance(self.topology, RecipeTopology):
            topology = self.topology.to_dict()
        else:
            topology = self.topology

        model_capabilities: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.model_capabilities, Unset):
            model_capabilities = self.model_capabilities.to_dict()

        recipe_capabilities: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.recipe_capabilities, Unset):
            recipe_capabilities = self.recipe_capabilities.to_dict()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "definition": definition,
            "generated_at": generated_at,
            "model_documents": model_documents,
            "operational_state": operational_state,
            "placement": placement,
            "reasons": reasons,
            "recipe": recipe,
            "topology": topology,
        })
        if model_capabilities is not UNSET:
            field_dict["model_capabilities"] = model_capabilities
        if recipe_capabilities is not UNSET:
            field_dict["recipe_capabilities"] = recipe_capabilities
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operational_state import OperationalState
        from ..models.library_recipe_identity import LibraryRecipeIdentity
        from ..models.library_recipe_model import LibraryRecipeModel
        from ..models.library_capability_inventory import LibraryCapabilityInventory
        from ..models.recipe_topology import RecipeTopology
        from ..models.topology_placement import TopologyPlacement
        from ..models.recipe_definition import RecipeDefinition
        from ..models.library_projection_reason import LibraryProjectionReason
        d = dict(src_dict)
        definition = RecipeDefinition.from_dict(d.pop("definition"))




        generated_at = isoparse(d.pop("generated_at"))




        model_documents = []
        _model_documents = d.pop("model_documents")
        for model_documents_item_data in (_model_documents):
            model_documents_item = LibraryRecipeModel.from_dict(model_documents_item_data)



            model_documents.append(model_documents_item)


        operational_state = OperationalState.from_dict(d.pop("operational_state"))




        placement = []
        _placement = d.pop("placement")
        for placement_item_data in (_placement):
            placement_item = TopologyPlacement.from_dict(placement_item_data)



            placement.append(placement_item)


        reasons = []
        _reasons = d.pop("reasons")
        for reasons_item_data in (_reasons):
            reasons_item = LibraryProjectionReason.from_dict(reasons_item_data)



            reasons.append(reasons_item)


        recipe = LibraryRecipeIdentity.from_dict(d.pop("recipe"))




        def _parse_topology(data: object) -> Union['RecipeTopology', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                topology_type_0 = RecipeTopology.from_dict(data)



                return topology_type_0
            except: # noqa: E722
                pass
            return cast(Union['RecipeTopology', None], data)

        topology = _parse_topology(d.pop("topology"))


        _model_capabilities = d.pop("model_capabilities", UNSET)
        model_capabilities: Union[Unset, LibraryCapabilityInventory]
        if isinstance(_model_capabilities,  Unset):
            model_capabilities = UNSET
        else:
            model_capabilities = LibraryCapabilityInventory.from_dict(_model_capabilities)




        _recipe_capabilities = d.pop("recipe_capabilities", UNSET)
        recipe_capabilities: Union[Unset, LibraryCapabilityInventory]
        if isinstance(_recipe_capabilities,  Unset):
            recipe_capabilities = UNSET
        else:
            recipe_capabilities = LibraryCapabilityInventory.from_dict(_recipe_capabilities)




        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        library_recipe_detail = cls(
            definition=definition,
            generated_at=generated_at,
            model_documents=model_documents,
            operational_state=operational_state,
            placement=placement,
            reasons=reasons,
            recipe=recipe,
            topology=topology,
            model_capabilities=model_capabilities,
            recipe_capabilities=recipe_capabilities,
            schema_version=schema_version,
        )

        return library_recipe_detail
