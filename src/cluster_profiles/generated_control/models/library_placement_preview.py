from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_placement_preview_desired_state import check_library_placement_preview_desired_state
from ..models.library_placement_preview_desired_state import LibraryPlacementPreviewDesiredState
from ..models.library_placement_preview_invocation import check_library_placement_preview_invocation
from ..models.library_placement_preview_invocation import LibraryPlacementPreviewInvocation
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
import datetime

if TYPE_CHECKING:
  from ..models.library_placement_node import LibraryPlacementNode
  from ..models.library_placement_locations import LibraryPlacementLocations
  from ..models.library_placement_reason import LibraryPlacementReason
  from ..models.library_placement_step import LibraryPlacementStep





T = TypeVar("T", bound="LibraryPlacementPreview")



@_attrs_define
class LibraryPlacementPreview:
    """
        Attributes:
            alias (Union[None, str]):
            allowed (bool):
            blockers (list['LibraryPlacementReason']):
            desired_state (LibraryPlacementPreviewDesiredState):
            generated_at (datetime.datetime):
            invocation (LibraryPlacementPreviewInvocation):
            locations (LibraryPlacementLocations):
            plan_digest (str):
            recipe_id (str):
            recipe_revision_id (str):
            recipe_title (str):
            selected_node_ids (list[str]):
            selected_nodes (list['LibraryPlacementNode']):
            steps (list['LibraryPlacementStep']):
            topology_name (str):
            warnings (list['LibraryPlacementReason']):
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    alias: Union[None, str]
    allowed: bool
    blockers: list['LibraryPlacementReason']
    desired_state: LibraryPlacementPreviewDesiredState
    generated_at: datetime.datetime
    invocation: LibraryPlacementPreviewInvocation
    locations: 'LibraryPlacementLocations'
    plan_digest: str
    recipe_id: str
    recipe_revision_id: str
    recipe_title: str
    selected_node_ids: list[str]
    selected_nodes: list['LibraryPlacementNode']
    steps: list['LibraryPlacementStep']
    topology_name: str
    warnings: list['LibraryPlacementReason']
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_placement_node import LibraryPlacementNode
        from ..models.library_placement_locations import LibraryPlacementLocations
        from ..models.library_placement_reason import LibraryPlacementReason
        from ..models.library_placement_step import LibraryPlacementStep
        alias: Union[None, str]
        alias = self.alias

        allowed = self.allowed

        blockers = []
        for blockers_item_data in self.blockers:
            blockers_item = blockers_item_data.to_dict()
            blockers.append(blockers_item)



        desired_state: str = self.desired_state

        generated_at = self.generated_at.isoformat()

        invocation: str = self.invocation

        locations = self.locations.to_dict()

        plan_digest = self.plan_digest

        recipe_id = self.recipe_id

        recipe_revision_id = self.recipe_revision_id

        recipe_title = self.recipe_title

        selected_node_ids = self.selected_node_ids



        selected_nodes = []
        for selected_nodes_item_data in self.selected_nodes:
            selected_nodes_item = selected_nodes_item_data.to_dict()
            selected_nodes.append(selected_nodes_item)



        steps = []
        for steps_item_data in self.steps:
            steps_item = steps_item_data.to_dict()
            steps.append(steps_item)



        topology_name = self.topology_name

        warnings = []
        for warnings_item_data in self.warnings:
            warnings_item = warnings_item_data.to_dict()
            warnings.append(warnings_item)



        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "alias": alias,
            "allowed": allowed,
            "blockers": blockers,
            "desired_state": desired_state,
            "generated_at": generated_at,
            "invocation": invocation,
            "locations": locations,
            "plan_digest": plan_digest,
            "recipe_id": recipe_id,
            "recipe_revision_id": recipe_revision_id,
            "recipe_title": recipe_title,
            "selected_node_ids": selected_node_ids,
            "selected_nodes": selected_nodes,
            "steps": steps,
            "topology_name": topology_name,
            "warnings": warnings,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_placement_node import LibraryPlacementNode
        from ..models.library_placement_locations import LibraryPlacementLocations
        from ..models.library_placement_reason import LibraryPlacementReason
        from ..models.library_placement_step import LibraryPlacementStep
        d = dict(src_dict)
        def _parse_alias(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        alias = _parse_alias(d.pop("alias"))


        allowed = d.pop("allowed")

        blockers = []
        _blockers = d.pop("blockers")
        for blockers_item_data in (_blockers):
            blockers_item = LibraryPlacementReason.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        desired_state = check_library_placement_preview_desired_state(d.pop("desired_state"))




        generated_at = isoparse(d.pop("generated_at"))




        invocation = check_library_placement_preview_invocation(d.pop("invocation"))




        locations = LibraryPlacementLocations.from_dict(d.pop("locations"))




        plan_digest = d.pop("plan_digest")

        recipe_id = d.pop("recipe_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        recipe_title = d.pop("recipe_title")

        selected_node_ids = cast(list[str], d.pop("selected_node_ids"))


        selected_nodes = []
        _selected_nodes = d.pop("selected_nodes")
        for selected_nodes_item_data in (_selected_nodes):
            selected_nodes_item = LibraryPlacementNode.from_dict(selected_nodes_item_data)



            selected_nodes.append(selected_nodes_item)


        steps = []
        _steps = d.pop("steps")
        for steps_item_data in (_steps):
            steps_item = LibraryPlacementStep.from_dict(steps_item_data)



            steps.append(steps_item)


        topology_name = d.pop("topology_name")

        warnings = []
        _warnings = d.pop("warnings")
        for warnings_item_data in (_warnings):
            warnings_item = LibraryPlacementReason.from_dict(warnings_item_data)



            warnings.append(warnings_item)


        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        library_placement_preview = cls(
            alias=alias,
            allowed=allowed,
            blockers=blockers,
            desired_state=desired_state,
            generated_at=generated_at,
            invocation=invocation,
            locations=locations,
            plan_digest=plan_digest,
            recipe_id=recipe_id,
            recipe_revision_id=recipe_revision_id,
            recipe_title=recipe_title,
            selected_node_ids=selected_node_ids,
            selected_nodes=selected_nodes,
            steps=steps,
            topology_name=topology_name,
            warnings=warnings,
            schema_version=schema_version,
        )

        return library_placement_preview
