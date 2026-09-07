from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_library_placement_context_desired_state import check_fleet_profile_library_placement_context_desired_state
from ..models.fleet_profile_library_placement_context_desired_state import FleetProfileLibraryPlacementContextDesiredState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="FleetProfileLibraryPlacementContext")



@_attrs_define
class FleetProfileLibraryPlacementContext:
    """ Identity binding retained for replay of a direct Library placement.

        Attributes:
            desired_state (FleetProfileLibraryPlacementContextDesiredState):
            plan_digest (str):
            profile_plan_digest (str):
            recipe_id (str):
            recipe_revision_id (str):
            selected_node_ids (list[str]):
            alias (Union[None, Unset, str]):
            installation_ids (Union[Unset, list[str]]):
            run_ids (Union[Unset, list[str]]):
     """

    desired_state: FleetProfileLibraryPlacementContextDesiredState
    plan_digest: str
    profile_plan_digest: str
    recipe_id: str
    recipe_revision_id: str
    selected_node_ids: list[str]
    alias: Union[None, Unset, str] = UNSET
    installation_ids: Union[Unset, list[str]] = UNSET
    run_ids: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        desired_state: str = self.desired_state

        plan_digest = self.plan_digest

        profile_plan_digest = self.profile_plan_digest

        recipe_id = self.recipe_id

        recipe_revision_id = self.recipe_revision_id

        selected_node_ids = self.selected_node_ids



        alias: Union[None, Unset, str]
        if isinstance(self.alias, Unset):
            alias = UNSET
        else:
            alias = self.alias

        installation_ids: Union[Unset, list[str]] = UNSET
        if not isinstance(self.installation_ids, Unset):
            installation_ids = self.installation_ids



        run_ids: Union[Unset, list[str]] = UNSET
        if not isinstance(self.run_ids, Unset):
            run_ids = self.run_ids




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "desired_state": desired_state,
            "plan_digest": plan_digest,
            "profile_plan_digest": profile_plan_digest,
            "recipe_id": recipe_id,
            "recipe_revision_id": recipe_revision_id,
            "selected_node_ids": selected_node_ids,
        })
        if alias is not UNSET:
            field_dict["alias"] = alias
        if installation_ids is not UNSET:
            field_dict["installation_ids"] = installation_ids
        if run_ids is not UNSET:
            field_dict["run_ids"] = run_ids

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        desired_state = check_fleet_profile_library_placement_context_desired_state(d.pop("desired_state"))




        plan_digest = d.pop("plan_digest")

        profile_plan_digest = d.pop("profile_plan_digest")

        recipe_id = d.pop("recipe_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        selected_node_ids = cast(list[str], d.pop("selected_node_ids"))


        def _parse_alias(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        alias = _parse_alias(d.pop("alias", UNSET))


        installation_ids = cast(list[str], d.pop("installation_ids", UNSET))


        run_ids = cast(list[str], d.pop("run_ids", UNSET))


        fleet_profile_library_placement_context = cls(
            desired_state=desired_state,
            plan_digest=plan_digest,
            profile_plan_digest=profile_plan_digest,
            recipe_id=recipe_id,
            recipe_revision_id=recipe_revision_id,
            selected_node_ids=selected_node_ids,
            alias=alias,
            installation_ids=installation_ids,
            run_ids=run_ids,
        )

        return fleet_profile_library_placement_context
