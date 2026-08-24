from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PublicRecipeTopologyRole")



@_attrs_define
class PublicRecipeTopologyRole:
    """
        Attributes:
            count (int):
            endpoint_owner (bool):
            name (str):
     """

    count: int
    endpoint_owner: bool
    name: str





    def to_dict(self) -> dict[str, Any]:
        count = self.count

        endpoint_owner = self.endpoint_owner

        name = self.name


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "count": count,
            "endpoint_owner": endpoint_owner,
            "name": name,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        count = d.pop("count")

        endpoint_owner = d.pop("endpoint_owner")

        name = d.pop("name")

        public_recipe_topology_role = cls(
            count=count,
            endpoint_owner=endpoint_owner,
            name=name,
        )

        return public_recipe_topology_role
