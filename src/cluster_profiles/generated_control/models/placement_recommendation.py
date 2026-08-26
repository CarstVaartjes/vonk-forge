from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.placement_recommendation_install_state import check_placement_recommendation_install_state
from ..models.placement_recommendation_install_state import PlacementRecommendationInstallState
from ..models.placement_recommendation_load_state import check_placement_recommendation_load_state
from ..models.placement_recommendation_load_state import PlacementRecommendationLoadState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.placement_node import PlacementNode
  from ..models.image_distribution_preview_target import ImageDistributionPreviewTarget
  from ..models.build_preview_target import BuildPreviewTarget
  from ..models.mapping_preview_target import MappingPreviewTarget
  from ..models.placement_score import PlacementScore
  from ..models.install_preview_target import InstallPreviewTarget
  from ..models.run_preview_target import RunPreviewTarget
  from ..models.library_projection_reason import LibraryProjectionReason





T = TypeVar("T", bound="PlacementRecommendation")



@_attrs_define
class PlacementRecommendation:
    """
        Attributes:
            eligible (bool):
            install_state (PlacementRecommendationInstallState):
            installation_ids (list[str]):
            load_state (PlacementRecommendationLoadState):
            mapping_id (Union[None, str]):
            node_ids (list[str]):
            nodes (list['PlacementNode']):
            preview_targets (list[Union['BuildPreviewTarget', 'ImageDistributionPreviewTarget', 'InstallPreviewTarget',
                'MappingPreviewTarget', 'RunPreviewTarget']]):
            reasons (list['LibraryProjectionReason']):
            recipe_build_id (Union[None, str]):
            recipe_revision_id (str):
            run_ids (list[str]):
            score (PlacementScore):
            topology_name (str):
            group_complete (Union[Unset, bool]):  Default: True.
            ranking_scope (Union[Literal['bounded-advisory'], Unset]):  Default: 'bounded-advisory'.
     """

    eligible: bool
    install_state: PlacementRecommendationInstallState
    installation_ids: list[str]
    load_state: PlacementRecommendationLoadState
    mapping_id: Union[None, str]
    node_ids: list[str]
    nodes: list['PlacementNode']
    preview_targets: list[Union['BuildPreviewTarget', 'ImageDistributionPreviewTarget', 'InstallPreviewTarget', 'MappingPreviewTarget', 'RunPreviewTarget']]
    reasons: list['LibraryProjectionReason']
    recipe_build_id: Union[None, str]
    recipe_revision_id: str
    run_ids: list[str]
    score: 'PlacementScore'
    topology_name: str
    group_complete: Union[Unset, bool] = True
    ranking_scope: Union[Literal['bounded-advisory'], Unset] = 'bounded-advisory'





    def to_dict(self) -> dict[str, Any]:
        from ..models.placement_node import PlacementNode
        from ..models.image_distribution_preview_target import ImageDistributionPreviewTarget
        from ..models.build_preview_target import BuildPreviewTarget
        from ..models.mapping_preview_target import MappingPreviewTarget
        from ..models.placement_score import PlacementScore
        from ..models.install_preview_target import InstallPreviewTarget
        from ..models.run_preview_target import RunPreviewTarget
        from ..models.library_projection_reason import LibraryProjectionReason
        eligible = self.eligible

        install_state: str = self.install_state

        installation_ids = self.installation_ids



        load_state: str = self.load_state

        mapping_id: Union[None, str]
        mapping_id = self.mapping_id

        node_ids = self.node_ids



        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        preview_targets = []
        for preview_targets_item_data in self.preview_targets:
            preview_targets_item: dict[str, Any]
            if isinstance(preview_targets_item_data, BuildPreviewTarget):
                preview_targets_item = preview_targets_item_data.to_dict()
            elif isinstance(preview_targets_item_data, MappingPreviewTarget):
                preview_targets_item = preview_targets_item_data.to_dict()
            elif isinstance(preview_targets_item_data, ImageDistributionPreviewTarget):
                preview_targets_item = preview_targets_item_data.to_dict()
            elif isinstance(preview_targets_item_data, InstallPreviewTarget):
                preview_targets_item = preview_targets_item_data.to_dict()
            else:
                preview_targets_item = preview_targets_item_data.to_dict()

            preview_targets.append(preview_targets_item)



        reasons = []
        for reasons_item_data in self.reasons:
            reasons_item = reasons_item_data.to_dict()
            reasons.append(reasons_item)



        recipe_build_id: Union[None, str]
        recipe_build_id = self.recipe_build_id

        recipe_revision_id = self.recipe_revision_id

        run_ids = self.run_ids



        score = self.score.to_dict()

        topology_name = self.topology_name

        group_complete = self.group_complete

        ranking_scope = self.ranking_scope


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "eligible": eligible,
            "install_state": install_state,
            "installation_ids": installation_ids,
            "load_state": load_state,
            "mapping_id": mapping_id,
            "node_ids": node_ids,
            "nodes": nodes,
            "preview_targets": preview_targets,
            "reasons": reasons,
            "recipe_build_id": recipe_build_id,
            "recipe_revision_id": recipe_revision_id,
            "run_ids": run_ids,
            "score": score,
            "topology_name": topology_name,
        })
        if group_complete is not UNSET:
            field_dict["group_complete"] = group_complete
        if ranking_scope is not UNSET:
            field_dict["ranking_scope"] = ranking_scope

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.placement_node import PlacementNode
        from ..models.image_distribution_preview_target import ImageDistributionPreviewTarget
        from ..models.build_preview_target import BuildPreviewTarget
        from ..models.mapping_preview_target import MappingPreviewTarget
        from ..models.placement_score import PlacementScore
        from ..models.install_preview_target import InstallPreviewTarget
        from ..models.run_preview_target import RunPreviewTarget
        from ..models.library_projection_reason import LibraryProjectionReason
        d = dict(src_dict)
        eligible = d.pop("eligible")

        install_state = check_placement_recommendation_install_state(d.pop("install_state"))




        installation_ids = cast(list[str], d.pop("installation_ids"))


        load_state = check_placement_recommendation_load_state(d.pop("load_state"))




        def _parse_mapping_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        mapping_id = _parse_mapping_id(d.pop("mapping_id"))


        node_ids = cast(list[str], d.pop("node_ids"))


        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = PlacementNode.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        preview_targets = []
        _preview_targets = d.pop("preview_targets")
        for preview_targets_item_data in (_preview_targets):
            def _parse_preview_targets_item(data: object) -> Union['BuildPreviewTarget', 'ImageDistributionPreviewTarget', 'InstallPreviewTarget', 'MappingPreviewTarget', 'RunPreviewTarget']:
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    preview_targets_item_type_0 = BuildPreviewTarget.from_dict(data)



                    return preview_targets_item_type_0
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    preview_targets_item_type_1 = MappingPreviewTarget.from_dict(data)



                    return preview_targets_item_type_1
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    preview_targets_item_type_2 = ImageDistributionPreviewTarget.from_dict(data)



                    return preview_targets_item_type_2
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    preview_targets_item_type_3 = InstallPreviewTarget.from_dict(data)



                    return preview_targets_item_type_3
                except: # noqa: E722
                    pass
                if not isinstance(data, dict):
                    raise TypeError()
                preview_targets_item_type_4 = RunPreviewTarget.from_dict(data)



                return preview_targets_item_type_4

            preview_targets_item = _parse_preview_targets_item(preview_targets_item_data)

            preview_targets.append(preview_targets_item)


        reasons = []
        _reasons = d.pop("reasons")
        for reasons_item_data in (_reasons):
            reasons_item = LibraryProjectionReason.from_dict(reasons_item_data)



            reasons.append(reasons_item)


        def _parse_recipe_build_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        recipe_build_id = _parse_recipe_build_id(d.pop("recipe_build_id"))


        recipe_revision_id = d.pop("recipe_revision_id")

        run_ids = cast(list[str], d.pop("run_ids"))


        score = PlacementScore.from_dict(d.pop("score"))




        topology_name = d.pop("topology_name")

        group_complete = d.pop("group_complete", UNSET)

        ranking_scope = cast(Union[Literal['bounded-advisory'], Unset] , d.pop("ranking_scope", UNSET))
        if ranking_scope != 'bounded-advisory' and not isinstance(ranking_scope, Unset):
            raise ValueError(f"ranking_scope must match const 'bounded-advisory', got '{ranking_scope}'")

        placement_recommendation = cls(
            eligible=eligible,
            install_state=install_state,
            installation_ids=installation_ids,
            load_state=load_state,
            mapping_id=mapping_id,
            node_ids=node_ids,
            nodes=nodes,
            preview_targets=preview_targets,
            reasons=reasons,
            recipe_build_id=recipe_build_id,
            recipe_revision_id=recipe_revision_id,
            run_ids=run_ids,
            score=score,
            topology_name=topology_name,
            group_complete=group_complete,
            ranking_scope=ranking_scope,
        )

        return placement_recommendation
