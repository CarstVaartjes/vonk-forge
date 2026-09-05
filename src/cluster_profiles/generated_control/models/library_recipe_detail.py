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
  from ..models.model_version_identity import ModelVersionIdentity
  from ..models.library_model_version_facts import LibraryModelVersionFacts
  from ..models.operational_state import OperationalState
  from ..models.library_recipe_identity import LibraryRecipeIdentity
  from ..models.recipe_revision_summary import RecipeRevisionSummary
  from ..models.library_capability_inventory import LibraryCapabilityInventory
  from ..models.recipe_topology import RecipeTopology
  from ..models.topology_placement import TopologyPlacement
  from ..models.library_projection_reason import LibraryProjectionReason
  from ..models.visual_recipe_document import VisualRecipeDocument





T = TypeVar("T", bound="LibraryRecipeDetail")



@_attrs_define
class LibraryRecipeDetail:
    """
        Attributes:
            generated_at (datetime.datetime):
            operational_state (OperationalState):
            placement (list['TopologyPlacement']):
            reasons (list['LibraryProjectionReason']):
            recipe (LibraryRecipeIdentity):
            selected_revision (Union['RecipeRevisionSummary', None]):
            topology (Union['RecipeTopology', None]):
            visual_recipe (Union['VisualRecipeDocument', None]):
            model (Union['ModelVersionIdentity', None, Unset]):
            model_capabilities (Union[Unset, LibraryCapabilityInventory]): Compare-friendly model or recipe capability
                assertions with evidence state.
            model_version (Union['LibraryModelVersionFacts', None, Unset]):
            recipe_capabilities (Union[Unset, LibraryCapabilityInventory]): Compare-friendly model or recipe capability
                assertions with evidence state.
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    generated_at: datetime.datetime
    operational_state: 'OperationalState'
    placement: list['TopologyPlacement']
    reasons: list['LibraryProjectionReason']
    recipe: 'LibraryRecipeIdentity'
    selected_revision: Union['RecipeRevisionSummary', None]
    topology: Union['RecipeTopology', None]
    visual_recipe: Union['VisualRecipeDocument', None]
    model: Union['ModelVersionIdentity', None, Unset] = UNSET
    model_capabilities: Union[Unset, 'LibraryCapabilityInventory'] = UNSET
    model_version: Union['LibraryModelVersionFacts', None, Unset] = UNSET
    recipe_capabilities: Union[Unset, 'LibraryCapabilityInventory'] = UNSET
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        from ..models.model_version_identity import ModelVersionIdentity
        from ..models.library_model_version_facts import LibraryModelVersionFacts
        from ..models.operational_state import OperationalState
        from ..models.library_recipe_identity import LibraryRecipeIdentity
        from ..models.recipe_revision_summary import RecipeRevisionSummary
        from ..models.library_capability_inventory import LibraryCapabilityInventory
        from ..models.recipe_topology import RecipeTopology
        from ..models.topology_placement import TopologyPlacement
        from ..models.library_projection_reason import LibraryProjectionReason
        from ..models.visual_recipe_document import VisualRecipeDocument
        generated_at = self.generated_at.isoformat()

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

        selected_revision: Union[None, dict[str, Any]]
        if isinstance(self.selected_revision, RecipeRevisionSummary):
            selected_revision = self.selected_revision.to_dict()
        else:
            selected_revision = self.selected_revision

        topology: Union[None, dict[str, Any]]
        if isinstance(self.topology, RecipeTopology):
            topology = self.topology.to_dict()
        else:
            topology = self.topology

        visual_recipe: Union[None, dict[str, Any]]
        if isinstance(self.visual_recipe, VisualRecipeDocument):
            visual_recipe = self.visual_recipe.to_dict()
        else:
            visual_recipe = self.visual_recipe

        model: Union[None, Unset, dict[str, Any]]
        if isinstance(self.model, Unset):
            model = UNSET
        elif isinstance(self.model, ModelVersionIdentity):
            model = self.model.to_dict()
        else:
            model = self.model

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

        recipe_capabilities: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.recipe_capabilities, Unset):
            recipe_capabilities = self.recipe_capabilities.to_dict()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "generated_at": generated_at,
            "operational_state": operational_state,
            "placement": placement,
            "reasons": reasons,
            "recipe": recipe,
            "selected_revision": selected_revision,
            "topology": topology,
            "visual_recipe": visual_recipe,
        })
        if model is not UNSET:
            field_dict["model"] = model
        if model_capabilities is not UNSET:
            field_dict["model_capabilities"] = model_capabilities
        if model_version is not UNSET:
            field_dict["model_version"] = model_version
        if recipe_capabilities is not UNSET:
            field_dict["recipe_capabilities"] = recipe_capabilities
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.model_version_identity import ModelVersionIdentity
        from ..models.library_model_version_facts import LibraryModelVersionFacts
        from ..models.operational_state import OperationalState
        from ..models.library_recipe_identity import LibraryRecipeIdentity
        from ..models.recipe_revision_summary import RecipeRevisionSummary
        from ..models.library_capability_inventory import LibraryCapabilityInventory
        from ..models.recipe_topology import RecipeTopology
        from ..models.topology_placement import TopologyPlacement
        from ..models.library_projection_reason import LibraryProjectionReason
        from ..models.visual_recipe_document import VisualRecipeDocument
        d = dict(src_dict)
        generated_at = isoparse(d.pop("generated_at"))




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




        def _parse_selected_revision(data: object) -> Union['RecipeRevisionSummary', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                selected_revision_type_0 = RecipeRevisionSummary.from_dict(data)



                return selected_revision_type_0
            except: # noqa: E722
                pass
            return cast(Union['RecipeRevisionSummary', None], data)

        selected_revision = _parse_selected_revision(d.pop("selected_revision"))


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


        def _parse_visual_recipe(data: object) -> Union['VisualRecipeDocument', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                visual_recipe_type_0 = VisualRecipeDocument.from_dict(data)



                return visual_recipe_type_0
            except: # noqa: E722
                pass
            return cast(Union['VisualRecipeDocument', None], data)

        visual_recipe = _parse_visual_recipe(d.pop("visual_recipe"))


        def _parse_model(data: object) -> Union['ModelVersionIdentity', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                model_type_0 = ModelVersionIdentity.from_dict(data)



                return model_type_0
            except: # noqa: E722
                pass
            return cast(Union['ModelVersionIdentity', None, Unset], data)

        model = _parse_model(d.pop("model", UNSET))


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


        _recipe_capabilities = d.pop("recipe_capabilities", UNSET)
        recipe_capabilities: Union[Unset, LibraryCapabilityInventory]
        if isinstance(_recipe_capabilities,  Unset):
            recipe_capabilities = UNSET
        else:
            recipe_capabilities = LibraryCapabilityInventory.from_dict(_recipe_capabilities)




        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        library_recipe_detail = cls(
            generated_at=generated_at,
            operational_state=operational_state,
            placement=placement,
            reasons=reasons,
            recipe=recipe,
            selected_revision=selected_revision,
            topology=topology,
            visual_recipe=visual_recipe,
            model=model,
            model_capabilities=model_capabilities,
            model_version=model_version,
            recipe_capabilities=recipe_capabilities,
            schema_version=schema_version,
        )

        return library_recipe_detail
