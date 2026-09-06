from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_runtime_environment import RecipeRuntimeEnvironment
  from ..models.recipe_runtime_argument import RecipeRuntimeArgument
  from ..models.recipe_lifecycle import RecipeLifecycle





T = TypeVar("T", bound="RecipeRuntime")



@_attrs_define
class RecipeRuntime:
    """
        Attributes:
            arguments (list['RecipeRuntimeArgument']):
            engine (str):
            entrypoint (list[str]):
            environment (list['RecipeRuntimeEnvironment']):
            lifecycle (RecipeLifecycle):
     """

    arguments: list['RecipeRuntimeArgument']
    engine: str
    entrypoint: list[str]
    environment: list['RecipeRuntimeEnvironment']
    lifecycle: 'RecipeLifecycle'





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_runtime_environment import RecipeRuntimeEnvironment
        from ..models.recipe_runtime_argument import RecipeRuntimeArgument
        from ..models.recipe_lifecycle import RecipeLifecycle
        arguments = []
        for arguments_item_data in self.arguments:
            arguments_item = arguments_item_data.to_dict()
            arguments.append(arguments_item)



        engine = self.engine

        entrypoint = self.entrypoint



        environment = []
        for environment_item_data in self.environment:
            environment_item = environment_item_data.to_dict()
            environment.append(environment_item)



        lifecycle = self.lifecycle.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "arguments": arguments,
            "engine": engine,
            "entrypoint": entrypoint,
            "environment": environment,
            "lifecycle": lifecycle,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_runtime_environment import RecipeRuntimeEnvironment
        from ..models.recipe_runtime_argument import RecipeRuntimeArgument
        from ..models.recipe_lifecycle import RecipeLifecycle
        d = dict(src_dict)
        arguments = []
        _arguments = d.pop("arguments")
        for arguments_item_data in (_arguments):
            arguments_item = RecipeRuntimeArgument.from_dict(arguments_item_data)



            arguments.append(arguments_item)


        engine = d.pop("engine")

        entrypoint = cast(list[str], d.pop("entrypoint"))


        environment = []
        _environment = d.pop("environment")
        for environment_item_data in (_environment):
            environment_item = RecipeRuntimeEnvironment.from_dict(environment_item_data)



            environment.append(environment_item)


        lifecycle = RecipeLifecycle.from_dict(d.pop("lifecycle"))




        recipe_runtime = cls(
            arguments=arguments,
            engine=engine,
            entrypoint=entrypoint,
            environment=environment,
            lifecycle=lifecycle,
        )

        return recipe_runtime
