from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_integer_setting_change_effect import check_recipe_integer_setting_change_effect
from ..models.recipe_integer_setting_change_effect import RecipeIntegerSettingChangeEffect
from typing import cast






T = TypeVar("T", bound="RecipeIntegerSetting")



@_attrs_define
class RecipeIntegerSetting:
    """
        Attributes:
            change_effect (RecipeIntegerSettingChangeEffect):
            value (int):
     """

    change_effect: RecipeIntegerSettingChangeEffect
    value: int





    def to_dict(self) -> dict[str, Any]:
        change_effect: str = self.change_effect

        value = self.value


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "change_effect": change_effect,
            "value": value,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        change_effect = check_recipe_integer_setting_change_effect(d.pop("change_effect"))




        value = d.pop("value")

        recipe_integer_setting = cls(
            change_effect=change_effect,
            value=value,
        )

        return recipe_integer_setting
