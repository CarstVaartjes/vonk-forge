from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="OperationalMappingNode")



@_attrs_define
class OperationalMappingNode:
    """
        Attributes:
            endpoint_owner (bool):
            node_id (str):
            rank (int):
            role (str):
     """

    endpoint_owner: bool
    node_id: str
    rank: int
    role: str





    def to_dict(self) -> dict[str, Any]:
        endpoint_owner = self.endpoint_owner

        node_id = self.node_id

        rank = self.rank

        role = self.role


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "endpoint_owner": endpoint_owner,
            "node_id": node_id,
            "rank": rank,
            "role": role,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        endpoint_owner = d.pop("endpoint_owner")

        node_id = d.pop("node_id")

        rank = d.pop("rank")

        role = d.pop("role")

        operational_mapping_node = cls(
            endpoint_owner=endpoint_owner,
            node_id=node_id,
            rank=rank,
            role=role,
        )

        return operational_mapping_node
