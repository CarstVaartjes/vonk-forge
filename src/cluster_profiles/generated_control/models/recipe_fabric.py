from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_fabric_connectivity import check_recipe_fabric_connectivity
from ..models.recipe_fabric_connectivity import RecipeFabricConnectivity
from typing import cast






T = TypeVar("T", bound="RecipeFabric")



@_attrs_define
class RecipeFabric:
    """
        Attributes:
            connectivity (RecipeFabricConnectivity):
            minimum_bandwidth_mbps (int):
     """

    connectivity: RecipeFabricConnectivity
    minimum_bandwidth_mbps: int





    def to_dict(self) -> dict[str, Any]:
        connectivity: str = self.connectivity

        minimum_bandwidth_mbps = self.minimum_bandwidth_mbps


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "connectivity": connectivity,
            "minimum_bandwidth_mbps": minimum_bandwidth_mbps,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        connectivity = check_recipe_fabric_connectivity(d.pop("connectivity"))




        minimum_bandwidth_mbps = d.pop("minimum_bandwidth_mbps")

        recipe_fabric = cls(
            connectivity=connectivity,
            minimum_bandwidth_mbps=minimum_bandwidth_mbps,
        )

        return recipe_fabric
