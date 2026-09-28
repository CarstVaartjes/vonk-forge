from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_role_resources import RecipeRoleResources





T = TypeVar("T", bound="RecipeTopologyRole")



@_attrs_define
class RecipeTopologyRole:
    """
        Attributes:
            count (int):
            endpoint_owner (bool):
            name (str):
            resources (RecipeRoleResources):
     """

    count: int
    endpoint_owner: bool
    name: str
    resources: RecipeRoleResources





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_role_resources import RecipeRoleResources # noqa: PLC0415
        count = self.count

        endpoint_owner = self.endpoint_owner

        name = self.name

        resources = self.resources.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "count": count,
            "endpoint_owner": endpoint_owner,
            "name": name,
            "resources": resources,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_role_resources import RecipeRoleResources # noqa: PLC0415
        d = dict(src_dict)
        count = d.pop("count")

        endpoint_owner = d.pop("endpoint_owner")

        name = d.pop("name")

        resources = RecipeRoleResources.from_dict(d.pop("resources"))




        recipe_topology_role = cls(
            count=count,
            endpoint_owner=endpoint_owner,
            name=name,
            resources=resources,
        )

        return recipe_topology_role
