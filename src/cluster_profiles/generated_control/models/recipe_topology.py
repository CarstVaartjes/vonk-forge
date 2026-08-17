from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_fabric import RecipeFabric
  from ..models.recipe_parallelism import RecipeParallelism
  from ..models.recipe_role import RecipeRole





T = TypeVar("T", bound="RecipeTopology")



@_attrs_define
class RecipeTopology:
    """
        Attributes:
            fabric (RecipeFabric):
            mode (str):
            name (str):
            node_count (int):
            parallelism (RecipeParallelism):
            roles (list['RecipeRole']):
            start_order (list[str]):
            stop_order (list[str]):
     """

    fabric: 'RecipeFabric'
    mode: str
    name: str
    node_count: int
    parallelism: 'RecipeParallelism'
    roles: list['RecipeRole']
    start_order: list[str]
    stop_order: list[str]





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_fabric import RecipeFabric
        from ..models.recipe_parallelism import RecipeParallelism
        from ..models.recipe_role import RecipeRole
        fabric = self.fabric.to_dict()

        mode = self.mode

        name = self.name

        node_count = self.node_count

        parallelism = self.parallelism.to_dict()

        roles = []
        for roles_item_data in self.roles:
            roles_item = roles_item_data.to_dict()
            roles.append(roles_item)



        start_order = self.start_order



        stop_order = self.stop_order




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "fabric": fabric,
            "mode": mode,
            "name": name,
            "node_count": node_count,
            "parallelism": parallelism,
            "roles": roles,
            "start_order": start_order,
            "stop_order": stop_order,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_fabric import RecipeFabric
        from ..models.recipe_parallelism import RecipeParallelism
        from ..models.recipe_role import RecipeRole
        d = dict(src_dict)
        fabric = RecipeFabric.from_dict(d.pop("fabric"))




        mode = d.pop("mode")

        name = d.pop("name")

        node_count = d.pop("node_count")

        parallelism = RecipeParallelism.from_dict(d.pop("parallelism"))




        roles = []
        _roles = d.pop("roles")
        for roles_item_data in (_roles):
            roles_item = RecipeRole.from_dict(roles_item_data)



            roles.append(roles_item)


        start_order = cast(list[str], d.pop("start_order"))


        stop_order = cast(list[str], d.pop("stop_order"))


        recipe_topology = cls(
            fabric=fabric,
            mode=mode,
            name=name,
            node_count=node_count,
            parallelism=parallelism,
            roles=roles,
            start_order=start_order,
            stop_order=stop_order,
        )

        return recipe_topology
