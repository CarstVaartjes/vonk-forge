from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_serving_validation import RecipeServingValidation





T = TypeVar("T", bound="RecipeValidation")



@_attrs_define
class RecipeValidation:
    """
        Attributes:
            serving (RecipeServingValidation):
     """

    serving: RecipeServingValidation





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_serving_validation import RecipeServingValidation # noqa: PLC0415
        serving = self.serving.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "serving": serving,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_serving_validation import RecipeServingValidation # noqa: PLC0415
        d = dict(src_dict)
        serving = RecipeServingValidation.from_dict(d.pop("serving"))




        recipe_validation = cls(
            serving=serving,
        )

        return recipe_validation
