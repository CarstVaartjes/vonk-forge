from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_assignment_view_model import FleetProfileAssignmentViewModel
  from ..models.fleet_profile_assignment_view_option_choices import FleetProfileAssignmentViewOptionChoices
  from ..models.fleet_profile_assignment_view_recipe import FleetProfileAssignmentViewRecipe
  from ..models.fleet_profile_assignment_view_resources import FleetProfileAssignmentViewResources
  from ..models.recipe_update_notice import RecipeUpdateNotice





T = TypeVar("T", bound="FleetProfileAssignmentView")



@_attrs_define
class FleetProfileAssignmentView:
    """ Canonical read projection of one logical profile assignment.

        Attributes:
            assigned_sparks (int):
            display_name (str):
            recipe_selector (str):
            selector (str):
            spark_ids (list[str]):
            model (FleetProfileAssignmentViewModel | Unset):
            observed_state (str | Unset):  Default: 'Not loaded'.
            option_choices (FleetProfileAssignmentViewOptionChoices | Unset):
            recipe (FleetProfileAssignmentViewRecipe | Unset):
            recipe_id (None | str | Unset):
            recipe_update (None | RecipeUpdateNotice | Unset):
            required_sparks (int | None | Unset):
            resources (FleetProfileAssignmentViewResources | Unset):
     """

    assigned_sparks: int
    display_name: str
    recipe_selector: str
    selector: str
    spark_ids: list[str]
    model: FleetProfileAssignmentViewModel | Unset = UNSET
    observed_state: str | Unset = 'Not loaded'
    option_choices: FleetProfileAssignmentViewOptionChoices | Unset = UNSET
    recipe: FleetProfileAssignmentViewRecipe | Unset = UNSET
    recipe_id: None | str | Unset = UNSET
    recipe_update: None | RecipeUpdateNotice | Unset = UNSET
    required_sparks: int | None | Unset = UNSET
    resources: FleetProfileAssignmentViewResources | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_assignment_view_model import FleetProfileAssignmentViewModel # noqa: PLC0415
        from ..models.fleet_profile_assignment_view_option_choices import FleetProfileAssignmentViewOptionChoices # noqa: PLC0415
        from ..models.fleet_profile_assignment_view_recipe import FleetProfileAssignmentViewRecipe # noqa: PLC0415
        from ..models.fleet_profile_assignment_view_resources import FleetProfileAssignmentViewResources # noqa: PLC0415
        from ..models.recipe_update_notice import RecipeUpdateNotice # noqa: PLC0415
        assigned_sparks = self.assigned_sparks

        display_name = self.display_name

        recipe_selector = self.recipe_selector

        selector = self.selector

        spark_ids = self.spark_ids



        model: dict[str, Any] | Unset = UNSET
        if not isinstance(self.model, Unset):
            model = self.model.to_dict()

        observed_state = self.observed_state

        option_choices: dict[str, Any] | Unset = UNSET
        if not isinstance(self.option_choices, Unset):
            option_choices = self.option_choices.to_dict()

        recipe: dict[str, Any] | Unset = UNSET
        if not isinstance(self.recipe, Unset):
            recipe = self.recipe.to_dict()

        recipe_id: None | str | Unset
        if isinstance(self.recipe_id, Unset):
            recipe_id = UNSET
        else:
            recipe_id = self.recipe_id

        recipe_update: dict[str, Any] | None | Unset
        if isinstance(self.recipe_update, Unset):
            recipe_update = UNSET
        elif isinstance(self.recipe_update, RecipeUpdateNotice):
            recipe_update = self.recipe_update.to_dict()
        else:
            recipe_update = self.recipe_update

        required_sparks: int | None | Unset
        if isinstance(self.required_sparks, Unset):
            required_sparks = UNSET
        else:
            required_sparks = self.required_sparks

        resources: dict[str, Any] | Unset = UNSET
        if not isinstance(self.resources, Unset):
            resources = self.resources.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assigned_sparks": assigned_sparks,
            "display_name": display_name,
            "recipe_selector": recipe_selector,
            "selector": selector,
            "spark_ids": spark_ids,
        })
        if model is not UNSET:
            field_dict["model"] = model
        if observed_state is not UNSET:
            field_dict["observed_state"] = observed_state
        if option_choices is not UNSET:
            field_dict["option_choices"] = option_choices
        if recipe is not UNSET:
            field_dict["recipe"] = recipe
        if recipe_id is not UNSET:
            field_dict["recipe_id"] = recipe_id
        if recipe_update is not UNSET:
            field_dict["recipe_update"] = recipe_update
        if required_sparks is not UNSET:
            field_dict["required_sparks"] = required_sparks
        if resources is not UNSET:
            field_dict["resources"] = resources

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_assignment_view_model import FleetProfileAssignmentViewModel # noqa: PLC0415
        from ..models.fleet_profile_assignment_view_option_choices import FleetProfileAssignmentViewOptionChoices # noqa: PLC0415
        from ..models.fleet_profile_assignment_view_recipe import FleetProfileAssignmentViewRecipe # noqa: PLC0415
        from ..models.fleet_profile_assignment_view_resources import FleetProfileAssignmentViewResources # noqa: PLC0415
        from ..models.recipe_update_notice import RecipeUpdateNotice # noqa: PLC0415
        d = dict(src_dict)
        assigned_sparks = d.pop("assigned_sparks")

        display_name = d.pop("display_name")

        recipe_selector = d.pop("recipe_selector")

        selector = d.pop("selector")

        spark_ids = cast(list[str], d.pop("spark_ids"))


        _model = d.pop("model", UNSET)
        model: FleetProfileAssignmentViewModel | Unset
        if isinstance(_model,  Unset):
            model = UNSET
        else:
            model = FleetProfileAssignmentViewModel.from_dict(_model)




        observed_state = d.pop("observed_state", UNSET)

        _option_choices = d.pop("option_choices", UNSET)
        option_choices: FleetProfileAssignmentViewOptionChoices | Unset
        if isinstance(_option_choices,  Unset):
            option_choices = UNSET
        else:
            option_choices = FleetProfileAssignmentViewOptionChoices.from_dict(_option_choices)




        _recipe = d.pop("recipe", UNSET)
        recipe: FleetProfileAssignmentViewRecipe | Unset
        if isinstance(_recipe,  Unset):
            recipe = UNSET
        else:
            recipe = FleetProfileAssignmentViewRecipe.from_dict(_recipe)




        def _parse_recipe_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        recipe_id = _parse_recipe_id(d.pop("recipe_id", UNSET))


        def _parse_recipe_update(data: object) -> None | RecipeUpdateNotice | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                recipe_update_type_0 = RecipeUpdateNotice.from_dict(data)



                return recipe_update_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecipeUpdateNotice | Unset, data)

        recipe_update = _parse_recipe_update(d.pop("recipe_update", UNSET))


        def _parse_required_sparks(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        required_sparks = _parse_required_sparks(d.pop("required_sparks", UNSET))


        _resources = d.pop("resources", UNSET)
        resources: FleetProfileAssignmentViewResources | Unset
        if isinstance(_resources,  Unset):
            resources = UNSET
        else:
            resources = FleetProfileAssignmentViewResources.from_dict(_resources)




        fleet_profile_assignment_view = cls(
            assigned_sparks=assigned_sparks,
            display_name=display_name,
            recipe_selector=recipe_selector,
            selector=selector,
            spark_ids=spark_ids,
            model=model,
            observed_state=observed_state,
            option_choices=option_choices,
            recipe=recipe,
            recipe_id=recipe_id,
            recipe_update=recipe_update,
            required_sparks=required_sparks,
            resources=resources,
        )

        return fleet_profile_assignment_view
