from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Union






T = TypeVar("T", bound="FleetProfileNode")



@_attrs_define
class FleetProfileNode:
    """
        Attributes:
            node_id (str):
            rank (int):
            role (str):
            endpoint_owner (Union[Unset, bool]):  Default: False.
     """

    node_id: str
    rank: int
    role: str
    endpoint_owner: Union[Unset, bool] = False





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        rank = self.rank

        role = self.role

        endpoint_owner = self.endpoint_owner


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "rank": rank,
            "role": role,
        })
        if endpoint_owner is not UNSET:
            field_dict["endpoint_owner"] = endpoint_owner

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = d.pop("node_id")

        rank = d.pop("rank")

        role = d.pop("role")

        endpoint_owner = d.pop("endpoint_owner", UNSET)

        fleet_profile_node = cls(
            node_id=node_id,
            rank=rank,
            role=role,
            endpoint_owner=endpoint_owner,
        )

        return fleet_profile_node
