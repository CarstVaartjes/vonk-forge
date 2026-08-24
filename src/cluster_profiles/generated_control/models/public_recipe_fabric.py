from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.public_recipe_fabric_connectivity import check_public_recipe_fabric_connectivity
from ..models.public_recipe_fabric_connectivity import PublicRecipeFabricConnectivity
from typing import cast






T = TypeVar("T", bound="PublicRecipeFabric")



@_attrs_define
class PublicRecipeFabric:
    """
        Attributes:
            connectivity (PublicRecipeFabricConnectivity):
            minimum_bandwidth_mbps (int):
     """

    connectivity: PublicRecipeFabricConnectivity
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
        connectivity = check_public_recipe_fabric_connectivity(d.pop("connectivity"))




        minimum_bandwidth_mbps = d.pop("minimum_bandwidth_mbps")

        public_recipe_fabric = cls(
            connectivity=connectivity,
            minimum_bandwidth_mbps=minimum_bandwidth_mbps,
        )

        return public_recipe_fabric
