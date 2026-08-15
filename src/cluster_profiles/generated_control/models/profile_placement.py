from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.placement_limits import PlacementLimits
  from ..models.library_projection_reason import LibraryProjectionReason
  from ..models.rejected_node import RejectedNode
  from ..models.placement_evidence_counts import PlacementEvidenceCounts
  from ..models.placement_recommendation import PlacementRecommendation





T = TypeVar("T", bound="ProfilePlacement")



@_attrs_define
class ProfilePlacement:
    """
        Attributes:
            candidate_node_ids (list[str]):
            evaluated_group_count (int):
            evidence_counts (PlacementEvidenceCounts):
            limits (PlacementLimits):
            node_count (int):
            profile_name (str):
            reasons (list['LibraryProjectionReason']):
            recommendations (list['PlacementRecommendation']):
            rejected_evidence_truncated (bool):
            rejected_groups (list['PlacementRecommendation']):
            rejected_nodes (list['RejectedNode']):
            search_complete (bool):
     """

    candidate_node_ids: list[str]
    evaluated_group_count: int
    evidence_counts: 'PlacementEvidenceCounts'
    limits: 'PlacementLimits'
    node_count: int
    profile_name: str
    reasons: list['LibraryProjectionReason']
    recommendations: list['PlacementRecommendation']
    rejected_evidence_truncated: bool
    rejected_groups: list['PlacementRecommendation']
    rejected_nodes: list['RejectedNode']
    search_complete: bool





    def to_dict(self) -> dict[str, Any]:
        from ..models.placement_limits import PlacementLimits
        from ..models.library_projection_reason import LibraryProjectionReason
        from ..models.rejected_node import RejectedNode
        from ..models.placement_evidence_counts import PlacementEvidenceCounts
        from ..models.placement_recommendation import PlacementRecommendation
        candidate_node_ids = self.candidate_node_ids



        evaluated_group_count = self.evaluated_group_count

        evidence_counts = self.evidence_counts.to_dict()

        limits = self.limits.to_dict()

        node_count = self.node_count

        profile_name = self.profile_name

        reasons = []
        for reasons_item_data in self.reasons:
            reasons_item = reasons_item_data.to_dict()
            reasons.append(reasons_item)



        recommendations = []
        for recommendations_item_data in self.recommendations:
            recommendations_item = recommendations_item_data.to_dict()
            recommendations.append(recommendations_item)



        rejected_evidence_truncated = self.rejected_evidence_truncated

        rejected_groups = []
        for rejected_groups_item_data in self.rejected_groups:
            rejected_groups_item = rejected_groups_item_data.to_dict()
            rejected_groups.append(rejected_groups_item)



        rejected_nodes = []
        for rejected_nodes_item_data in self.rejected_nodes:
            rejected_nodes_item = rejected_nodes_item_data.to_dict()
            rejected_nodes.append(rejected_nodes_item)



        search_complete = self.search_complete


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "candidate_node_ids": candidate_node_ids,
            "evaluated_group_count": evaluated_group_count,
            "evidence_counts": evidence_counts,
            "limits": limits,
            "node_count": node_count,
            "profile_name": profile_name,
            "reasons": reasons,
            "recommendations": recommendations,
            "rejected_evidence_truncated": rejected_evidence_truncated,
            "rejected_groups": rejected_groups,
            "rejected_nodes": rejected_nodes,
            "search_complete": search_complete,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.placement_limits import PlacementLimits
        from ..models.library_projection_reason import LibraryProjectionReason
        from ..models.rejected_node import RejectedNode
        from ..models.placement_evidence_counts import PlacementEvidenceCounts
        from ..models.placement_recommendation import PlacementRecommendation
        d = dict(src_dict)
        candidate_node_ids = cast(list[str], d.pop("candidate_node_ids"))


        evaluated_group_count = d.pop("evaluated_group_count")

        evidence_counts = PlacementEvidenceCounts.from_dict(d.pop("evidence_counts"))




        limits = PlacementLimits.from_dict(d.pop("limits"))




        node_count = d.pop("node_count")

        profile_name = d.pop("profile_name")

        reasons = []
        _reasons = d.pop("reasons")
        for reasons_item_data in (_reasons):
            reasons_item = LibraryProjectionReason.from_dict(reasons_item_data)



            reasons.append(reasons_item)


        recommendations = []
        _recommendations = d.pop("recommendations")
        for recommendations_item_data in (_recommendations):
            recommendations_item = PlacementRecommendation.from_dict(recommendations_item_data)



            recommendations.append(recommendations_item)


        rejected_evidence_truncated = d.pop("rejected_evidence_truncated")

        rejected_groups = []
        _rejected_groups = d.pop("rejected_groups")
        for rejected_groups_item_data in (_rejected_groups):
            rejected_groups_item = PlacementRecommendation.from_dict(rejected_groups_item_data)



            rejected_groups.append(rejected_groups_item)


        rejected_nodes = []
        _rejected_nodes = d.pop("rejected_nodes")
        for rejected_nodes_item_data in (_rejected_nodes):
            rejected_nodes_item = RejectedNode.from_dict(rejected_nodes_item_data)



            rejected_nodes.append(rejected_nodes_item)


        search_complete = d.pop("search_complete")

        profile_placement = cls(
            candidate_node_ids=candidate_node_ids,
            evaluated_group_count=evaluated_group_count,
            evidence_counts=evidence_counts,
            limits=limits,
            node_count=node_count,
            profile_name=profile_name,
            reasons=reasons,
            recommendations=recommendations,
            rejected_evidence_truncated=rejected_evidence_truncated,
            rejected_groups=rejected_groups,
            rejected_nodes=rejected_nodes,
            search_complete=search_complete,
        )

        return profile_placement
