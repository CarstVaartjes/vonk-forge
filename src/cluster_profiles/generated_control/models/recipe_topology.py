from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_parallelism import RecipeParallelism
  from ..models.recipe_topology_role import RecipeTopologyRole





T = TypeVar("T", bound="RecipeTopology")



@_attrs_define
class RecipeTopology:
    """ Roles and their start order; everything else follows from node_count.

    One node runs alone. More nodes share one connected fabric: losing a rank
    withdraws the endpoint, recovery restarts the workers and then the
    entrypoint, and stopping always starts with the endpoint owner.

        Attributes:
            name (str):
            node_count (int):
            parallelism (RecipeParallelism):
            roles (list[RecipeTopologyRole]):
            start_order (list[str]):
     """

    name: str
    node_count: int
    parallelism: RecipeParallelism
    roles: list[RecipeTopologyRole]
    start_order: list[str]





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_parallelism import RecipeParallelism # noqa: PLC0415
        from ..models.recipe_topology_role import RecipeTopologyRole # noqa: PLC0415
        name = self.name

        node_count = self.node_count

        parallelism = self.parallelism.to_dict()

        roles = []
        for roles_item_data in self.roles:
            roles_item = roles_item_data.to_dict()
            roles.append(roles_item)



        start_order = self.start_order




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "name": name,
            "node_count": node_count,
            "parallelism": parallelism,
            "roles": roles,
            "start_order": start_order,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_parallelism import RecipeParallelism # noqa: PLC0415
        from ..models.recipe_topology_role import RecipeTopologyRole # noqa: PLC0415
        d = dict(src_dict)
        name = d.pop("name")

        node_count = d.pop("node_count")

        parallelism = RecipeParallelism.from_dict(d.pop("parallelism"))




        roles = []
        _roles = d.pop("roles")
        for roles_item_data in (_roles):
            roles_item = RecipeTopologyRole.from_dict(roles_item_data)



            roles.append(roles_item)


        start_order = cast(list[str], d.pop("start_order"))


        recipe_topology = cls(
            name=name,
            node_count=node_count,
            parallelism=parallelism,
            roles=roles,
            start_order=start_order,
        )

        return recipe_topology
