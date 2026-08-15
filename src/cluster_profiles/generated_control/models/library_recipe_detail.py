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
import datetime

if TYPE_CHECKING:
  from ..models.profile_placement import ProfilePlacement
  from ..models.operational_state import OperationalState
  from ..models.library_projection_reason import LibraryProjectionReason
  from ..models.library_recipe_identity import LibraryRecipeIdentity
  from ..models.recipe_revision_summary import RecipeRevisionSummary
  from ..models.recipe_profile import RecipeProfile
  from ..models.visual_recipe_document import VisualRecipeDocument





T = TypeVar("T", bound="LibraryRecipeDetail")



@_attrs_define
class LibraryRecipeDetail:
    """
        Attributes:
            generated_at (datetime.datetime):
            operational_state (OperationalState):
            placement (list['ProfilePlacement']):
            profiles (list['RecipeProfile']):
            reasons (list['LibraryProjectionReason']):
            recipe (LibraryRecipeIdentity):
            selected_revision (Union['RecipeRevisionSummary', None]):
            visual_recipe (Union['VisualRecipeDocument', None]):
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    generated_at: datetime.datetime
    operational_state: 'OperationalState'
    placement: list['ProfilePlacement']
    profiles: list['RecipeProfile']
    reasons: list['LibraryProjectionReason']
    recipe: 'LibraryRecipeIdentity'
    selected_revision: Union['RecipeRevisionSummary', None]
    visual_recipe: Union['VisualRecipeDocument', None]
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        from ..models.profile_placement import ProfilePlacement
        from ..models.operational_state import OperationalState
        from ..models.library_projection_reason import LibraryProjectionReason
        from ..models.library_recipe_identity import LibraryRecipeIdentity
        from ..models.recipe_revision_summary import RecipeRevisionSummary
        from ..models.recipe_profile import RecipeProfile
        from ..models.visual_recipe_document import VisualRecipeDocument
        generated_at = self.generated_at.isoformat()

        operational_state = self.operational_state.to_dict()

        placement = []
        for placement_item_data in self.placement:
            placement_item = placement_item_data.to_dict()
            placement.append(placement_item)



        profiles = []
        for profiles_item_data in self.profiles:
            profiles_item = profiles_item_data.to_dict()
            profiles.append(profiles_item)



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

        visual_recipe: Union[None, dict[str, Any]]
        if isinstance(self.visual_recipe, VisualRecipeDocument):
            visual_recipe = self.visual_recipe.to_dict()
        else:
            visual_recipe = self.visual_recipe

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "generated_at": generated_at,
            "operational_state": operational_state,
            "placement": placement,
            "profiles": profiles,
            "reasons": reasons,
            "recipe": recipe,
            "selected_revision": selected_revision,
            "visual_recipe": visual_recipe,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.profile_placement import ProfilePlacement
        from ..models.operational_state import OperationalState
        from ..models.library_projection_reason import LibraryProjectionReason
        from ..models.library_recipe_identity import LibraryRecipeIdentity
        from ..models.recipe_revision_summary import RecipeRevisionSummary
        from ..models.recipe_profile import RecipeProfile
        from ..models.visual_recipe_document import VisualRecipeDocument
        d = dict(src_dict)
        generated_at = isoparse(d.pop("generated_at"))




        operational_state = OperationalState.from_dict(d.pop("operational_state"))




        placement = []
        _placement = d.pop("placement")
        for placement_item_data in (_placement):
            placement_item = ProfilePlacement.from_dict(placement_item_data)



            placement.append(placement_item)


        profiles = []
        _profiles = d.pop("profiles")
        for profiles_item_data in (_profiles):
            profiles_item = RecipeProfile.from_dict(profiles_item_data)



            profiles.append(profiles_item)


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


        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        library_recipe_detail = cls(
            generated_at=generated_at,
            operational_state=operational_state,
            placement=placement,
            profiles=profiles,
            reasons=reasons,
            recipe=recipe,
            selected_revision=selected_revision,
            visual_recipe=visual_recipe,
            schema_version=schema_version,
        )

        return library_recipe_detail
