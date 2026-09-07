from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_benchmark import RecipeBenchmark
  from ..models.recipe_serving_validation import RecipeServingValidation





T = TypeVar("T", bound="RecipeValidation")



@_attrs_define
class RecipeValidation:
    """
        Attributes:
            benchmarks (list['RecipeBenchmark']):
            serving (RecipeServingValidation):
     """

    benchmarks: list['RecipeBenchmark']
    serving: 'RecipeServingValidation'





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_benchmark import RecipeBenchmark
        from ..models.recipe_serving_validation import RecipeServingValidation
        benchmarks = []
        for benchmarks_item_data in self.benchmarks:
            benchmarks_item = benchmarks_item_data.to_dict()
            benchmarks.append(benchmarks_item)



        serving = self.serving.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "benchmarks": benchmarks,
            "serving": serving,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_benchmark import RecipeBenchmark
        from ..models.recipe_serving_validation import RecipeServingValidation
        d = dict(src_dict)
        benchmarks = []
        _benchmarks = d.pop("benchmarks")
        for benchmarks_item_data in (_benchmarks):
            benchmarks_item = RecipeBenchmark.from_dict(benchmarks_item_data)



            benchmarks.append(benchmarks_item)


        serving = RecipeServingValidation.from_dict(d.pop("serving"))




        recipe_validation = cls(
            benchmarks=benchmarks,
            serving=serving,
        )

        return recipe_validation
