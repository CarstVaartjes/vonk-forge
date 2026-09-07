from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_benchmark_configuration import RecipeBenchmarkConfiguration





T = TypeVar("T", bound="RecipeBenchmark")



@_attrs_define
class RecipeBenchmark:
    """
        Attributes:
            configuration (RecipeBenchmarkConfiguration):
            framework (str):
            name (str):
     """

    configuration: 'RecipeBenchmarkConfiguration'
    framework: str
    name: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_benchmark_configuration import RecipeBenchmarkConfiguration
        configuration = self.configuration.to_dict()

        framework = self.framework

        name = self.name


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "configuration": configuration,
            "framework": framework,
            "name": name,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_benchmark_configuration import RecipeBenchmarkConfiguration
        d = dict(src_dict)
        configuration = RecipeBenchmarkConfiguration.from_dict(d.pop("configuration"))




        framework = d.pop("framework")

        name = d.pop("name")

        recipe_benchmark = cls(
            configuration=configuration,
            framework=framework,
            name=name,
        )

        return recipe_benchmark
