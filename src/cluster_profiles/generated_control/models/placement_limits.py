from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, Union, cast






T = TypeVar("T", bound="PlacementLimits")



@_attrs_define
class PlacementLimits:
    """
        Attributes:
            artifact_evidence_per_node_limit (Union[Literal[512], Unset]):  Default: 512.
            candidate_node_limit (Union[Literal[32], Unset]):  Default: 32.
            examined_group_limit (Union[Literal[512], Unset]):  Default: 512.
            operational_member_evidence_limit (Union[Literal[16384], Unset]):  Default: 16384.
            operational_row_evidence_limit (Union[Literal[512], Unset]):  Default: 512.
            recommendation_limit (Union[Literal[16], Unset]):  Default: 16.
            rejected_group_evidence_limit (Union[Literal[16], Unset]):  Default: 16.
            rejected_node_evidence_limit (Union[Literal[32], Unset]):  Default: 32.
     """

    artifact_evidence_per_node_limit: Union[Literal[512], Unset] = 512
    candidate_node_limit: Union[Literal[32], Unset] = 32
    examined_group_limit: Union[Literal[512], Unset] = 512
    operational_member_evidence_limit: Union[Literal[16384], Unset] = 16384
    operational_row_evidence_limit: Union[Literal[512], Unset] = 512
    recommendation_limit: Union[Literal[16], Unset] = 16
    rejected_group_evidence_limit: Union[Literal[16], Unset] = 16
    rejected_node_evidence_limit: Union[Literal[32], Unset] = 32





    def to_dict(self) -> dict[str, Any]:
        artifact_evidence_per_node_limit = self.artifact_evidence_per_node_limit

        candidate_node_limit = self.candidate_node_limit

        examined_group_limit = self.examined_group_limit

        operational_member_evidence_limit = self.operational_member_evidence_limit

        operational_row_evidence_limit = self.operational_row_evidence_limit

        recommendation_limit = self.recommendation_limit

        rejected_group_evidence_limit = self.rejected_group_evidence_limit

        rejected_node_evidence_limit = self.rejected_node_evidence_limit


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if artifact_evidence_per_node_limit is not UNSET:
            field_dict["artifact_evidence_per_node_limit"] = artifact_evidence_per_node_limit
        if candidate_node_limit is not UNSET:
            field_dict["candidate_node_limit"] = candidate_node_limit
        if examined_group_limit is not UNSET:
            field_dict["examined_group_limit"] = examined_group_limit
        if operational_member_evidence_limit is not UNSET:
            field_dict["operational_member_evidence_limit"] = operational_member_evidence_limit
        if operational_row_evidence_limit is not UNSET:
            field_dict["operational_row_evidence_limit"] = operational_row_evidence_limit
        if recommendation_limit is not UNSET:
            field_dict["recommendation_limit"] = recommendation_limit
        if rejected_group_evidence_limit is not UNSET:
            field_dict["rejected_group_evidence_limit"] = rejected_group_evidence_limit
        if rejected_node_evidence_limit is not UNSET:
            field_dict["rejected_node_evidence_limit"] = rejected_node_evidence_limit

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_evidence_per_node_limit = cast(Union[Literal[512], Unset] , d.pop("artifact_evidence_per_node_limit", UNSET))
        if artifact_evidence_per_node_limit != 512and not isinstance(artifact_evidence_per_node_limit, Unset):
            raise ValueError(f"artifact_evidence_per_node_limit must match const 512, got '{artifact_evidence_per_node_limit}'")

        candidate_node_limit = cast(Union[Literal[32], Unset] , d.pop("candidate_node_limit", UNSET))
        if candidate_node_limit != 32and not isinstance(candidate_node_limit, Unset):
            raise ValueError(f"candidate_node_limit must match const 32, got '{candidate_node_limit}'")

        examined_group_limit = cast(Union[Literal[512], Unset] , d.pop("examined_group_limit", UNSET))
        if examined_group_limit != 512and not isinstance(examined_group_limit, Unset):
            raise ValueError(f"examined_group_limit must match const 512, got '{examined_group_limit}'")

        operational_member_evidence_limit = cast(Union[Literal[16384], Unset] , d.pop("operational_member_evidence_limit", UNSET))
        if operational_member_evidence_limit != 16384and not isinstance(operational_member_evidence_limit, Unset):
            raise ValueError(f"operational_member_evidence_limit must match const 16384, got '{operational_member_evidence_limit}'")

        operational_row_evidence_limit = cast(Union[Literal[512], Unset] , d.pop("operational_row_evidence_limit", UNSET))
        if operational_row_evidence_limit != 512and not isinstance(operational_row_evidence_limit, Unset):
            raise ValueError(f"operational_row_evidence_limit must match const 512, got '{operational_row_evidence_limit}'")

        recommendation_limit = cast(Union[Literal[16], Unset] , d.pop("recommendation_limit", UNSET))
        if recommendation_limit != 16and not isinstance(recommendation_limit, Unset):
            raise ValueError(f"recommendation_limit must match const 16, got '{recommendation_limit}'")

        rejected_group_evidence_limit = cast(Union[Literal[16], Unset] , d.pop("rejected_group_evidence_limit", UNSET))
        if rejected_group_evidence_limit != 16and not isinstance(rejected_group_evidence_limit, Unset):
            raise ValueError(f"rejected_group_evidence_limit must match const 16, got '{rejected_group_evidence_limit}'")

        rejected_node_evidence_limit = cast(Union[Literal[32], Unset] , d.pop("rejected_node_evidence_limit", UNSET))
        if rejected_node_evidence_limit != 32and not isinstance(rejected_node_evidence_limit, Unset):
            raise ValueError(f"rejected_node_evidence_limit must match const 32, got '{rejected_node_evidence_limit}'")

        placement_limits = cls(
            artifact_evidence_per_node_limit=artifact_evidence_per_node_limit,
            candidate_node_limit=candidate_node_limit,
            examined_group_limit=examined_group_limit,
            operational_member_evidence_limit=operational_member_evidence_limit,
            operational_row_evidence_limit=operational_row_evidence_limit,
            recommendation_limit=recommendation_limit,
            rejected_group_evidence_limit=rejected_group_evidence_limit,
            rejected_node_evidence_limit=rejected_node_evidence_limit,
        )

        return placement_limits
