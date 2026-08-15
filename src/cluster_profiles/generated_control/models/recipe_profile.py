from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_profile_measurement import check_recipe_profile_measurement
from ..models.recipe_profile_measurement import RecipeProfileMeasurement
from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_fabric import RecipeFabric
  from ..models.recipe_parallelism import RecipeParallelism
  from ..models.recipe_profile_parameter_overrides import RecipeProfileParameterOverrides
  from ..models.recipe_role import RecipeRole





T = TypeVar("T", bound="RecipeProfile")



@_attrs_define
class RecipeProfile:
    """
        Attributes:
            description (str):
            fabric (RecipeFabric):
            measurement (RecipeProfileMeasurement):
            name (str):
            node_count (int):
            parallelism (RecipeParallelism):
            parameter_overrides (RecipeProfileParameterOverrides):
            roles (list['RecipeRole']):
            strategy (str):
     """

    description: str
    fabric: 'RecipeFabric'
    measurement: RecipeProfileMeasurement
    name: str
    node_count: int
    parallelism: 'RecipeParallelism'
    parameter_overrides: 'RecipeProfileParameterOverrides'
    roles: list['RecipeRole']
    strategy: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_fabric import RecipeFabric
        from ..models.recipe_parallelism import RecipeParallelism
        from ..models.recipe_profile_parameter_overrides import RecipeProfileParameterOverrides
        from ..models.recipe_role import RecipeRole
        description = self.description

        fabric = self.fabric.to_dict()

        measurement: str = self.measurement

        name = self.name

        node_count = self.node_count

        parallelism = self.parallelism.to_dict()

        parameter_overrides = self.parameter_overrides.to_dict()

        roles = []
        for roles_item_data in self.roles:
            roles_item = roles_item_data.to_dict()
            roles.append(roles_item)



        strategy = self.strategy


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "description": description,
            "fabric": fabric,
            "measurement": measurement,
            "name": name,
            "node_count": node_count,
            "parallelism": parallelism,
            "parameter_overrides": parameter_overrides,
            "roles": roles,
            "strategy": strategy,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_fabric import RecipeFabric
        from ..models.recipe_parallelism import RecipeParallelism
        from ..models.recipe_profile_parameter_overrides import RecipeProfileParameterOverrides
        from ..models.recipe_role import RecipeRole
        d = dict(src_dict)
        description = d.pop("description")

        fabric = RecipeFabric.from_dict(d.pop("fabric"))




        measurement = check_recipe_profile_measurement(d.pop("measurement"))




        name = d.pop("name")

        node_count = d.pop("node_count")

        parallelism = RecipeParallelism.from_dict(d.pop("parallelism"))




        parameter_overrides = RecipeProfileParameterOverrides.from_dict(d.pop("parameter_overrides"))




        roles = []
        _roles = d.pop("roles")
        for roles_item_data in (_roles):
            roles_item = RecipeRole.from_dict(roles_item_data)



            roles.append(roles_item)


        strategy = d.pop("strategy")

        recipe_profile = cls(
            description=description,
            fabric=fabric,
            measurement=measurement,
            name=name,
            node_count=node_count,
            parallelism=parallelism,
            parameter_overrides=parameter_overrides,
            roles=roles,
            strategy=strategy,
        )

        return recipe_profile
