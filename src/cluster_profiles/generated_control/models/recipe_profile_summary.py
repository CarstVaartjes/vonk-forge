from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_profile_summary_fabric_connectivity import check_recipe_profile_summary_fabric_connectivity
from ..models.recipe_profile_summary_fabric_connectivity import RecipeProfileSummaryFabricConnectivity
from typing import cast






T = TypeVar("T", bound="RecipeProfileSummary")



@_attrs_define
class RecipeProfileSummary:
    """
        Attributes:
            description (str):
            fabric_connectivity (RecipeProfileSummaryFabricConnectivity):
            minimum_bandwidth_mbps (int):
            name (str):
            node_count (int):
            roles (list[str]):
     """

    description: str
    fabric_connectivity: RecipeProfileSummaryFabricConnectivity
    minimum_bandwidth_mbps: int
    name: str
    node_count: int
    roles: list[str]





    def to_dict(self) -> dict[str, Any]:
        description = self.description

        fabric_connectivity: str = self.fabric_connectivity

        minimum_bandwidth_mbps = self.minimum_bandwidth_mbps

        name = self.name

        node_count = self.node_count

        roles = self.roles




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "description": description,
            "fabric_connectivity": fabric_connectivity,
            "minimum_bandwidth_mbps": minimum_bandwidth_mbps,
            "name": name,
            "node_count": node_count,
            "roles": roles,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        description = d.pop("description")

        fabric_connectivity = check_recipe_profile_summary_fabric_connectivity(d.pop("fabric_connectivity"))




        minimum_bandwidth_mbps = d.pop("minimum_bandwidth_mbps")

        name = d.pop("name")

        node_count = d.pop("node_count")

        roles = cast(list[str], d.pop("roles"))


        recipe_profile_summary = cls(
            description=description,
            fabric_connectivity=fabric_connectivity,
            minimum_bandwidth_mbps=minimum_bandwidth_mbps,
            name=name,
            node_count=node_count,
            roles=roles,
        )

        return recipe_profile_summary
